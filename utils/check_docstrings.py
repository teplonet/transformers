# coding=utf-8
# Copyright 2020 The HuggingFace Inc. team.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""
Utility that checks all docstrings of public objects have an argument section matching their signature.

Use from the root of the repo with:

```bash
python utils/check_docstrings.py
```

for a check that will error in case of inconsistencies (used by `make repo-consistency`).

To auto-fix issues run:

```bash
python utils/check_docstrings.py --fix_and_overwrite
```

which is used by `make fix-copies` (note that this fills what it cans, you might have to manually fill information
like argument descriptions).
"""
import argparse
import ast
import enum
import inspect
import operator as op
import re
from pathlib import Path
from typing import Any, Optional, Tuple, Union

from transformers.utils import direct_transformers_import
from check_repo import ignore_undocumented


PATH_TO_TRANSFORMERS = Path("src").resolve() / "transformers"

# This is to make sure the transformers module imported is the one in the repo.
transformers = direct_transformers_import(PATH_TO_TRANSFORMERS)

OPTIONAL_KEYWORD = "*optional*"
# Re pattern that catches args blocks in docstrings (with all variation around the name supported).
_re_args = re.compile(r"^\s*(Args?|Arguments?|Attributes?|Params?|Parameters?):\s*$")
# Re pattern that parses the start of an arg block: catches <name> (<description>) in those lines.
_re_parse_arg = re.compile(r"^(\s*)(\S+)\s+\(([^\)]+)\)")
# Re pattern that parses the end of a description of an arg (catches the default in *optional*, defaults to xxx).
_re_parse_description = re.compile(r"\*optional\*, defaults to (.*)$")


OBJECTS_TO_IGNORE = [
    "GenerationConfig",
    "PretrainedConfig",
]

# Supported math operations when interpreting the value of defaults.
MATH_OPERATORS = {
    ast.Add: op.add,
    ast.Sub: op.sub,
    ast.Mult: op.mul,
    ast.Div: op.truediv,
    ast.Pow: op.pow,
    ast.BitXor: op.xor,
    ast.USub: op.neg
}

def find_indent(line: str) -> int:
    """
    Returns the number of spaces that start a line indent.
    """
    search = re.search(r"^(\s*)(?:\S|$)", line)
    if search is None:
        return 0
    return len(search.groups()[0])


def stringify_default(default: Any) -> str:
    """
    Returns the string representation of a default value, as used in docstring: numbers are left as is, all other
    objects are in backtiks.

    Args:
        default (`Any`): The default value to process

    Returns:
        `str`: The string representation of that default.
    """
    if isinstance(default, bool):
        # We need to test for bool first as a bool passes isinstance(xxx, (int, float))
        return f"`{default}`"
    elif isinstance(default, enum.Enum):
        # We need to test for enum first as an enum with int values will pass isinstance(xxx, (int, float))
        return f"`{str(default)}`"
    elif isinstance(default, int):
        return str(default)
    elif isinstance(default, float):
        return str(round(default, 2))
    elif isinstance(default, str):
        return str(default) if default.isnumeric() else f'`"{default}"`'
    elif isinstance(default, type):
        return f"`{default.__name__}`"
    else:
        return f"`{default}`"


def eval_math_expression(expression: str) -> Optional[Union[float, int]]:
    # Mainly taken from the excellent https://stackoverflow.com/a/9558001
    """
    Evaluate (safely) a mathematial expression and returns its value.

    Args:
        expression (`str`): The expression to evaluate.
    
    Returns:
        `Optional[Union[float, int]]`: Returns `None` if the evaluation fails in any way and the value computed
        otherwise.
    
    Example:

    ```py
    >>> eval_expr('2^6')
    4
    >>> eval_expr('2**6')
    64
    >>> eval_expr('1 + 2*3**(4^5) / (6 + -7)')
    -5.0
    ```
    """
    try:
        return eval_node(ast.parse(expression, mode="eval").body)
    except TypeError:
        return


def eval_node(node):
    if isinstance(node, ast.Num): # <number>
        return node.n
    elif isinstance(node, ast.BinOp): # <left> <operator> <right>
        return MATH_OPERATORS[type(node.op)](eval_node(node.left), eval_node(node.right))
    elif isinstance(node, ast.UnaryOp): # <operator> <operand> e.g., -1
        return MATH_OPERATORS[type(node.op)](eval_node(node.operand))
    else:
        raise TypeError(node)


def replace_default_in_arg_description(description: str, default: Any) -> str:
    """
    Catches the default value in the description of an argument inside a docstring and replaces it by the value passed.

    Args:
        description (`str`): The description of an argument in a docstring to process.
        default (`Any`): The default value that whould be in the docstring of that argument.
    
    Returns:
       `str`: The description updated with the new default value.
    """
    if default is inspect._empty:
        # No default, make sure the description doesn't have any either
        idx = description.find(OPTIONAL_KEYWORD)
        if idx != -1:
            description = description[:idx].rstrip()
            if description.endswith(","):
                description = description[:-1].rstrip()
    elif default is None:
        # Default None are not written, we just set `*optional*`. If there is default that is not None specified in the
        # description, we do not erase it (as sometimes we set the default to `None` because the default is a mutable
        # object).
        idx = description.find(OPTIONAL_KEYWORD)
        if idx == -1:
            description = f"{description}, {OPTIONAL_KEYWORD}"
        elif re.search(r"defaults to `?None`?", description) is not None:
            len_optional = len(OPTIONAL_KEYWORD)
            description = description[:idx + len_optional]
    else:
        str_default = None
        # For numbers we may have a default that is given by a math operation (1/255 is really popular). We don't
        # want to replace those by their actual values.
        if isinstance(default, (int, float)) and re.search("defaults to `?(.*?)(?:`|$)", description) is not None:
            # Grab the default and evaluate it.
            current_default = re.search("defaults to `?(.*?)(?:`|$)", description).groups()[0]
            if default == eval_math_expression(current_default):
                try:
                    # If it can be directly converted to the type of the default, it's a simple value
                    str_default = str(type(default)(current_default))
                except Exception:
                    # Otherwise there is a math operator so we add a code block.
                    str_default = f"`{current_default}`"

        if str_default is None:
            str_default = stringify_default(default)
        # Make sure default match
        if OPTIONAL_KEYWORD not in description:
            description = f"{description}, {OPTIONAL_KEYWORD}, defaults to {str_default}"
        elif _re_parse_description.search(description) is None:
            idx = description.find(OPTIONAL_KEYWORD)
            len_optional = len(OPTIONAL_KEYWORD)
            description = f"{description[:idx + len_optional]}, defaults to {str_default}"
        else:
            description =  _re_parse_description.sub(fr"*optional*, defaults to {str_default}", description)
    
    return description


def get_default_description(arg: inspect.Parameter) -> str:
    """
    Builds a default description for a parameter that was not documented.

    Args:
        arg (`inspect.Parameter`): The argument in the signature to generate a description for.

    Returns:
        `str`: The description.
    """
    if arg.annotation is inspect._empty:
        arg_type = "<fill_type>"
    elif hasattr(arg.annotation, "__name__"):
        arg_type = arg.annotation.__name__
    else:
        arg_type = str(arg.annotation)

    if arg.default is inspect._empty:
        return f"`{arg_type}`"
    elif arg.default is None:
        return f"`{arg_type}`, {OPTIONAL_KEYWORD}"
    else:
        str_default = stringify_default(arg.default)
        return f"`{arg_type}`, {OPTIONAL_KEYWORD}, defaults to {str_default}"


def match_docstring_with_signature(obj: Any) -> Optional[Tuple[str, str]]:
    """
    Matches the docstring of an object with its signature.

    Args:
        obj (`Any`): The object to process.
    
    Returns:
        `Optional[Tuple[str, str]]`: Returns `None` if there is no docstring or no parameters documented in the
        docstring, otherwise returns a tuple of two strings: the current documentation of the arguments in the
        docstring and the one matched with the signature.
    """
    if len(getattr(obj, "__doc__", "")) == 0:
        # Nothing to do, there is no docstring.
        return

    signature = inspect.signature(obj).parameters

    obj_doc_lines = obj.__doc__.split("\n")
    # Get to the line where we start documenting arguments
    idx = 0
    while idx < len(obj_doc_lines) and _re_args.search(obj_doc_lines[idx]) is None:
        idx += 1
    
    if idx == len(obj_doc_lines):
        # Nothing to do, no parameters are documented.
        return

    indent = find_indent(obj_doc_lines[idx])
    arguments = {}
    current_arg = None
    idx += 1
    start_idx = idx
    # Keep going until the arg section is finished (nonempty line at the same indent level) or the end of the docstring.
    while idx < len(obj_doc_lines) and (len(obj_doc_lines[idx].strip()) == 0 or find_indent(obj_doc_lines[idx]) > indent):
        if find_indent(obj_doc_lines[idx]) == indent + 4:
            # New argument -> let's generate the proper doc for it
            re_search_arg = _re_parse_arg.search(obj_doc_lines[idx])
            if re_search_arg is not None:
                _, name, description = re_search_arg.groups()
                current_arg = name
                if name in signature:
                    default = signature[name].default
                    if signature[name].kind is inspect._ParameterKind.VAR_KEYWORD:
                        default = None
                    new_description = replace_default_in_arg_description(description, default)
                else:
                    new_description = description
                init_doc = _re_parse_arg.sub(fr"\1\2 ({new_description})", obj_doc_lines[idx])
                arguments[current_arg] = [init_doc]
        elif current_arg is not None:
            arguments[current_arg].append(obj_doc_lines[idx])
        
        idx += 1
    
    # We went too far by one (perhaps more if there are a lot of new lines)
    idx -= 1
    while len(obj_doc_lines[idx].strip()) == 0:
        arguments[current_arg] = arguments[current_arg][:-1]
        idx -= 1
    # And we went too far by one again.
    idx += 1
    
    old_doc_arg = "\n".join(obj_doc_lines[start_idx: idx])
    
    arguments = {name: "\n".join(doc) for name, doc in arguments.items()}
    # Add missing arguments with a template
    for name in set(signature.keys()) - set(arguments.keys()):
        arg = signature[name]
        # We ignore private arguments or *args/**kwargs (unless they are documented by the user)
        if name.startswith("_") or arg.kind in [inspect._ParameterKind.VAR_KEYWORD, inspect._ParameterKind.VAR_POSITIONAL]:
            arguments[name] = ""
        else:
            arg_desc = get_default_description(arg)
            arguments[name] = " " * (indent + 4) + f"{name} ({arg_desc}): <fill_docstring>"

    # Arguments are sorted by the order in the signature
    new_param_docs = [arguments[name] for name in signature.keys() if len(arguments[name]) > 0]
    new_doc_arg = "\n".join(new_param_docs)
    
    return old_doc_arg, new_doc_arg


def fix_docstring(obj: Any, old_doc_args: str, new_doc_args: str):
    """
    Fixes the docstring of an object by replacing its arguments documentaiton by the one matched with the signature.

    Args:
        obj (`Any`):
            The object whose dostring we are fixing.
        old_doc_args (`str`):
            The current documentation of the parameters of `obj` in the docstring (as returned by
            `match_docstring_with_signature`).
        new_doc_args (`str`):
            The documentation of the parameters of `obj` matched with its signature (as returned by
            `match_docstring_with_signature`).
    """
    # Read the docstring in the source code and make sure we have the right part of the docstring
    source, line_number = inspect.getsourcelines(obj)

    # Get to the line where we start documenting arguments
    idx = 0
    while idx < len(source) and _re_args.search(source[idx]) is None:
        idx += 1
    
    if idx == len(source):
        # Args are not defined in the docstring of this object
        return
    
    # Get to the line where we stop documenting arguments
    indent = find_indent(source[idx])
    idx += 1
    start_idx = idx
    while idx < len(source) and (len(source[idx].strip()) == 0 or find_indent(source[idx]) > indent):
        idx += 1
    
    idx -= 1
    while len(source[idx].strip()) == 0:
        idx -= 1
    idx += 1
    
    if "".join(source[start_idx: idx])[:-1] != old_doc_args:
        # Args are not fully defined in the docstring of this object
        return
    
    # Find the source file.
    module = obj.__module__
    obj_file = PATH_TO_TRANSFORMERS
    for part in module.split(".")[1:]:
        obj_file = obj_file / part
    obj_file = obj_file.with_suffix(".py")
    
    with open(obj_file, "r", encoding="utf-8") as f:
        content = f.read()
    
    # Replace content
    lines = content.split("\n")
    lines = lines[:line_number + start_idx - 1] + [new_doc_args] + lines[line_number + idx - 1:]

    print(f"Fixing the docstring of {obj.__name__} in {obj_file}.")
    with open(obj_file, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


def check_docstrings(overwrite: bool = False):
    """
    Check docstrings of all public objects that are callables and are documented.

    Args:
        overwrite (`bool`, *optional*, defaults to `False`):
            Whether to fix inconsistencies or not.
    """
    failures = []
    hard_failures = []
    for name in dir(transformers):
        # Skip objects that are private or not documented.
        if name.startswith("_") or ignore_undocumented(name) or name in OBJECTS_TO_IGNORE:
            continue

        obj = getattr(transformers, name)
        if not callable(obj) or not isinstance(obj, type) or getattr(obj, "__doc__", None) is None:
            continue
    
        # Check docstring
        try:
            result = match_docstring_with_signature(obj)
            if result is not None:
                old_doc, new_doc = result
        except:
            hard_failures.append(name)
            continue
        if old_doc != new_doc:
            if overwrite:
                # Temporary: we only fix the ones with no tempaltes to fill
                if "<fill_type>" not in new_doc and "<fill_docstring>" not in new_doc:
                    fix_docstring(obj, old_doc, new_doc)
            else:
                failures.append(name)
    
    # Deal with errors
    error_message = ""
    if len(hard_failures) > 0:
        error_message += (
            "The argument part of the docstrings of the following objects could not be processed, check they are "
            "properly formatted."
        )
        error_message += "\n" + "\n".join([f"- {name}" for name in hard_failures])
    if len(failures) > 0:
        error_message += (
            "The following objects docstrings do not match their signature. Run `make fix-copies` to fix this."
        )
        error_message += "\n" + "\n".join([f"- {name}" for name in failures])
    
    if len(error_message) > 0:
        error_message = "There was at least one problem when checking docstrings of public objects.\n" + error_message
        raise ValueError(error_message)
    

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--fix_and_overwrite", action="store_true", help="Whether to fix inconsistencies.")
    args = parser.parse_args()

    check_docstrings(overwrite=args.fix_and_overwrite)