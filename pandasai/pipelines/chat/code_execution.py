import ast
import logging
import traceback
from typing import Any, Callable, Generator, List, Union

from pandasai.exceptions import InvalidLLMOutputType, InvalidOutputValueMismatch
from pandasai.pipelines.logic_unit_output import LogicUnitOutput
from pandasai.responses.response_serializer import ResponseSerializer

from ...exceptions import NoResultFoundError
from ...helpers.logger import Logger
from ...helpers.optional import get_environment
from ...helpers.output_validator import OutputValidator
from ...schemas.df_config import Config
from ..base_logic_unit import BaseLogicUnit
from ..pipeline_context import PipelineContext
from .code_cleaning import CodeExecutionContext
import pandas as pd


class CodeExecution(BaseLogicUnit):
    """
    Code Execution Stage
    """

    _dfs: List
    _config: Union[Config, dict]
    _additional_dependencies: List[dict] = []
    _current_code_executed: str = None
    _retry_if_fail: bool = False
    _ast_comparator_map: dict = {
        ast.Eq: "=",
        ast.NotEq: "!=",
        ast.Lt: "<",
        ast.LtE: "<=",
        ast.Gt: ">",
        ast.GtE: ">=",
        ast.Is: "is",
        ast.IsNot: "is not",
        ast.In: "in",
        ast.NotIn: "not in",
    }

    def __init__(
        self,
        on_failure: Callable[[str, Exception], None] = None,
        on_retry: Callable[[str, Exception], None] = None,
        **kwargs,
    ):
        super().__init__(**kwargs)
        self.on_failure = on_failure
        self.on_retry = on_retry

    def execute(self, input: Any, **kwargs) -> Any:
        """
        This method will return output according to
        Implementation.

        :param input: Your input data.
        :param kwargs: A dictionary of keyword arguments.
            - 'logger' (any): The logger for logging.
            - 'config' (Config): Global configurations for the test
            - 'context' (any): The execution context.

        :return: The result of the execution.
        """
        self.context: PipelineContext = kwargs.get("context")
        self._dfs = self.context.dfs
        self._config = self.context.config
        self._additional_dependencies = self.context.get("additional_dependencies", [])
        self._current_code_executed = self.context.get("current_code_executed")
        self.logger: Logger = kwargs.get("logger")

        # Execute the code
        code_context = CodeExecutionContext(self.context.get("last_prompt_id"))

        retry_count = 0
        code_to_run = input
        result = None
        while retry_count <= self.context.config.max_retries:
            try:
                result = self.execute_code(code_to_run, code_context)
                if self.context.get("output_type") != "" and (
                    output_helper := self.context.get("output_type")
                ):
                    (validation_ok, validation_errors) = OutputValidator.validate(
                        output_helper, result
                    )

                    if not validation_ok:
                        raise InvalidLLMOutputType(validation_errors)

                if not OutputValidator.validate_result(result):
                    raise InvalidOutputValueMismatch(
                        f'Value type {type(result["value"])} must match with type {result["type"]}'
                    )

                break

            except Exception as e:
                traceback_errors = traceback.format_exc()
                self.logger.log(f"Failed with error: {traceback_errors}", logging.ERROR)
                if self.on_failure:
                    self.on_failure(code_to_run, traceback_errors)

                if (
                    not self.context.config.use_error_correction_framework
                    or retry_count >= self.context.config.max_retries
                ):
                    raise e

                retry_count += 1

                self.logger.log(
                    f"Failed to execute code retrying with a correction framework "
                    f"[retry number: {retry_count}]",
                    level=logging.WARNING,
                )

                # TODO - Move this implement to main execute function
                # Temporarily done for test cases this is to be fixed move to the main function
                code_to_run = self._retry_run_code(
                    code_to_run, self.context, self.logger, e
                )

        return LogicUnitOutput(
            result,
            True,
            "Code Executed Successfully",
            {"content_type": "response", "value": ResponseSerializer.serialize(result)},
            final_track_output=True,
        )

    def execute_code(self, code: str, context: CodeExecutionContext) -> Any:
        """
        Execute the python code generated by LLMs to answer the question
        about the input dataframe. Run the code in the current context and return the
        result.

        Args:
            code (str): Python code to execute.
            context (CodeExecutionContext): Code Execution Context
                    with prompt id.

        Returns:
            Any: The result of the code execution. The type of the result depends
                on the generated code.

        """
        # List the required dfs, so we can avoid to run the connectors
        # if the code does not need them
        dfs = self._required_dfs(code)
        environment: dict = get_environment(self._additional_dependencies)
        environment["dfs"] = self._get_originals(dfs)
        if len(environment["dfs"]) == 1:
            environment["df"] = environment["dfs"][0]

        if self._config.direct_sql:
            environment["execute_sql_query"] = self._dfs[0].execute_direct_sql_query

        # Execute the code
        exec(code, environment)

        # Get the result
        if "result" not in environment:
            raise NoResultFoundError("No result returned")

        return environment["result"]

    def _required_dfs(self, code: str) -> List[str]:
        """
        List the index of the DataFrames that are needed to execute the code. The goal
        is to avoid to run the connectors if the code does not need them.

        Args:
            code (str): Python code to execute

        Returns:
            List[int]: A list of the index of the DataFrames that are needed to execute
            the code.
        """

        # Sometimes GPT-3.5/4 use a for loop to iterate over the dfs (even if there is only one)
        # or they concatenate the dfs. In this case we need all the dfs
        if "for df in dfs" in code or "pd.concat(dfs" in code:
            return self._dfs

        required_dfs = []
        for i, df in enumerate(self._dfs):
            if f"dfs[{i}]" in code:
                required_dfs.append(df)
            else:
                required_dfs.append(None)
        return required_dfs or self._dfs

    def _get_originals(self, dfs):
        """
        Get original dfs

        Args:
            dfs (list): List of dfs

        Returns:
            list: List of dfs
        """
        original_dfs = []
        for df in dfs:
            # TODO - Check why this None check is there
            if df is None:
                original_dfs.append(None)
                continue

            if isinstance(df, pd.DataFrame):
                original_dfs.append(df)
            else:
                # Execute to fetch only if not dataframe
                df.execute()
                original_dfs.append(df.pandas_df)

        return original_dfs

    def _retry_run_code(
        self,
        code: str,
        context: PipelineContext,
        logger: Logger,
        e: Exception,
    ) -> str:
        """
        A method to retry the code execution with error correction framework.

        Args:
            code (str): A python code
            context (PipelineContext) : Pipeline Context
            logger (Logger) : Logger
            e (Exception): An exception
            dataframes

        Returns (str): A python code
        """
        if self.on_retry:
            return self.on_retry(code, e)
        else:
            raise e

    @staticmethod
    def _tokenize_operand(operand_node: ast.expr) -> Generator[str, None, None]:
        """
        Utility generator function to get subscript slice constants.

        Args:
            operand_node (ast.expr):
                The node to be tokenized.
        Yields:
            str: Token string.

        Examples:
            >>> code = '''
            ... foo = [1, [2, 3], [[4, 5], [6, 7]]]
            ... print(foo[2][1][0])
            ... '''
            >>> tree = ast.parse(code)
            >>> res = CodeManager._tokenize_operand(tree.body[1].value.args[0])
            >>> print(list(res))
            ['foo', 2, 1, 0]
        """
        if isinstance(operand_node, ast.Call):
            yield operand_node.func.attr

        if isinstance(operand_node, ast.Subscript):
            slice_ = operand_node.slice.value
            yield from CodeExecution._tokenize_operand(operand_node.value)
            yield slice_

        if isinstance(operand_node, ast.Name):
            yield operand_node.id

        if isinstance(operand_node, ast.Constant):
            yield operand_node.value

    @staticmethod
    def _get_df_id_by_nearest_assignment(
        current_lineno: int, assignments: list[ast.Assign], target_name: str
    ):
        """
        Utility function to get df label by finding the nearest assignment.

        Sort assignment nodes list (copy of the list) by line number.
        Iterate over the assignment nodes list. If the assignment node's value
        looks like `dfs[<index>]` and target label equals to `target_name`,
        set `nearest_assignment` to "dfs[<index>]".

        Args:
            current_lineno (int): Number of the current processed line.
            assignments (list[ast.Assign]): List of assignment nodes.
            target_name (str): Name of the target variable. The assignment
                node is supposed to assign to this name.

        Returns:
            str: The string representing df label, looks like "dfs[<index>]".
        """
        nearest_assignment = None
        assignments = sorted(assignments, key=lambda node: node.lineno)
        for assignment in assignments:
            if assignment.lineno > current_lineno:
                return nearest_assignment
            try:
                is_subscript = isinstance(assignment.value, ast.Subscript)
                dfs_on_the_right = assignment.value.value.id == "dfs"
                assign_to_target = assignment.targets[0].id == target_name
                if is_subscript and dfs_on_the_right and assign_to_target:
                    nearest_assignment = f"dfs[{assignment.value.slice.value}]"
            except AttributeError:
                continue
