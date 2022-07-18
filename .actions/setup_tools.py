# Copyright The PyTorch Lightning team.
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
import glob
import logging
import os
import pathlib
import re
import shutil
import tarfile
import tempfile
import urllib.request
from importlib.util import module_from_spec, spec_from_file_location
from itertools import groupby
from types import ModuleType
from typing import List

_PROJECT_ROOT = os.path.dirname(os.path.dirname(__file__))
_PACKAGE_MAPPING = {"pytorch": "pytorch_lightning", "app": "lightning_app"}

# TODO: remove this once lightning-ui package is ready as a dependency
_LIGHTNING_FRONTEND_RELEASE_URL = "https://storage.googleapis.com/grid-packages/lightning-ui/v0.0.0/build.tar.gz"


def _load_py_module(name: str, location: str) -> ModuleType:
    spec = spec_from_file_location(name, location)
    assert spec, f"Failed to load module {name} from {location}"
    py = module_from_spec(spec)
    assert spec.loader, f"ModuleSpec.loader is None for {name} from {location}"
    spec.loader.exec_module(py)
    return py


def load_requirements(
    path_dir: str, file_name: str = "base.txt", comment_char: str = "#", unfreeze: bool = True
) -> List[str]:
    """Load requirements from a file.

    >>> path_req = os.path.join(_PROJECT_ROOT, "requirements")
    >>> load_requirements(path_req)  # doctest: +ELLIPSIS +NORMALIZE_WHITESPACE
    ['numpy...', 'torch...', ...]
    """
    with open(os.path.join(path_dir, file_name)) as file:
        lines = [ln.strip() for ln in file.readlines()]
    reqs = []
    for ln in lines:
        # filer all comments
        comment = ""
        if comment_char in ln:
            comment = ln[ln.index(comment_char) :]
            ln = ln[: ln.index(comment_char)]
        req = ln.strip()
        # skip directly installed dependencies
        if not req or req.startswith("http") or "@http" in req:
            continue
        # remove version restrictions unless they are strict
        if unfreeze and "<" in req and "strict" not in comment:
            req = re.sub(r",? *<=? *[\d\.\*]+", "", req).strip()
        reqs.append(req)
    return reqs


def load_readme_description(path_dir: str, homepage: str, version: str) -> str:
    """Load readme as decribtion.

    >>> load_readme_description(_PROJECT_ROOT, "", "")  # doctest: +ELLIPSIS +NORMALIZE_WHITESPACE
    '<div align="center">...'
    """
    path_readme = os.path.join(path_dir, "README.md")
    text = open(path_readme, encoding="utf-8").read()

    # drop images from readme
    text = text.replace("![PT to PL](docs/source/_static/images/general/pl_quick_start_full_compressed.gif)", "")

    # https://github.com/Lightning-AI/lightning/raw/master/docs/source/_static/images/lightning_module/pt_to_pl.png
    github_source_url = os.path.join(homepage, "raw", version)
    # replace relative repository path to absolute link to the release
    #  do not replace all "docs" as in the readme we reger some other sources with particular path to docs
    text = text.replace("docs/source/_static/", f"{os.path.join(github_source_url, 'docs/source/_static/')}")

    # readthedocs badge
    text = text.replace("badge/?version=stable", f"badge/?version={version}")
    text = text.replace("pytorch-lightning.readthedocs.io/en/stable/", f"pytorch-lightning.readthedocs.io/en/{version}")
    # codecov badge
    text = text.replace("/branch/master/graph/badge.svg", f"/release/{version}/graph/badge.svg")
    # replace github badges for release ones
    text = text.replace("badge.svg?branch=master&event=push", f"badge.svg?tag={version}")
    # Azure...
    text = text.replace("?branchName=master", f"?branchName=refs%2Ftags%2F{version}")
    text = re.sub(r"\?definitionId=\d+&branchName=master", f"?definitionId=2&branchName=refs%2Ftags%2F{version}", text)

    skip_begin = r"<!-- following section will be skipped from PyPI description -->"
    skip_end = r"<!-- end skipping PyPI description -->"
    # todo: wrap content as commented description
    text = re.sub(rf"{skip_begin}.+?{skip_end}", "<!--  -->", text, flags=re.IGNORECASE + re.DOTALL)

    # # https://github.com/Borda/pytorch-lightning/releases/download/1.1.0a6/codecov_badge.png
    # github_release_url = os.path.join(homepage, "releases", "download", version)
    # # download badge and replace url with local file
    # text = _parse_for_badge(text, github_release_url)
    return text


def replace_block_with_imports(lines: List[str], import_path: str, kword: str = "class") -> List[str]:
    """Parse a file and replace implementtaions bodies of function or class.

    >>> py_file = os.path.join(_PROJECT_ROOT, "src", "pytorch_lightning", "loggers", "logger.py")
    >>> import_path = ".".join(["pytorch_lightning", "loggers", "logger"])
    >>> with open(py_file, encoding="utf-8") as fp:
    ...     lines = [ln.rstrip() for ln in fp.readlines()]
    >>> lines = replace_block_with_imports(lines, import_path, "class")
    >>> lines = replace_block_with_imports(lines, import_path, "def")
    """
    body, tracking, skip_offset = [], False, 0
    for ln in lines:
        offset = len(ln) - len(ln.lstrip())
        # in case of mating the class args are multi-line
        if tracking and ln and offset <= skip_offset and not any(ln.lstrip().startswith(c) for c in ")]"):
            tracking = False
        if ln.lstrip().startswith(f"{kword} ") and not tracking:
            name = ln.replace(f"{kword} ", "").strip()
            idxs = [name.index(c) for c in ":(" if c in name]
            name = name[: min(idxs)]
            # skip private, TODO: consider skip even protected
            if not name.startswith("__"):
                body.append(f"{' ' * offset}from {import_path} import {name}  # noqa: F401")
            tracking, skip_offset = True, offset
            continue
        if not tracking:
            body.append(ln)
    return body


def replace_vars_with_imports(lines: List[str], import_path: str) -> List[str]:
    """Parse a file and replace variable filling with import.

    >>> py_file = os.path.join(_PROJECT_ROOT, "src", "pytorch_lightning", "utilities", "imports.py")
    >>> import_path = ".".join(["pytorch_lightning", "utilities", "imports"])
    >>> with open(py_file, encoding="utf-8") as fp:
    ...     lines = [ln.rstrip() for ln in fp.readlines()]
    >>> lines = replace_vars_with_imports(lines, import_path)
    """
    body, tracking, skip_offset = [], False, 0
    for ln in lines:
        offset = len(ln) - len(ln.lstrip())
        # in case of mating the class args are multi-line
        if tracking and ln and offset <= skip_offset and not any(ln.lstrip().startswith(c) for c in ")]}"):
            tracking = False
        var = re.match(r"^([\w_\d]+)[: [\w\., \[\]]*]? = ", ln.lstrip())
        if var:
            name = var.groups()[0]
            # skip private or apply white-list for allowed vars
            if not name.startswith("__") or name in ("__all__",):
                body.append(f"{' ' * offset}from {import_path} import {name}  # noqa: F401")
            tracking, skip_offset = True, offset
            continue
        if not tracking:
            body.append(ln)
    return body


def prune_imports_callables(lines: List[str]) -> List[str]:
    """Prune imports and calling functions from a file, even multi-line.

    >>> py_file = os.path.join(_PROJECT_ROOT, "src", "pytorch_lightning", "utilities", "cli.py")
    >>> import_path = ".".join(["pytorch_lightning", "utilities", "cli"])
    >>> with open(py_file, encoding="utf-8") as fp:
    ...     lines = [ln.rstrip() for ln in fp.readlines()]
    >>> lines = prune_imports_callables(lines)
    """
    body, tracking, skip_offset = [], False, 0
    for ln in lines:
        if ln.lstrip().startswith("import "):
            continue
        offset = len(ln) - len(ln.lstrip())
        # in case of mating the class args are multi-line
        if tracking and ln and offset <= skip_offset and not any(ln.lstrip().startswith(c) for c in ")]}"):
            tracking = False
        # catching callable
        call = re.match(r"^[\w_\d\.]+\(", ln.lstrip())
        if (ln.lstrip().startswith("from ") and " import " in ln) or call:
            tracking, skip_offset = True, offset
            continue
        if not tracking:
            body.append(ln)
    return body


def prune_empty_statements(lines: List[str]) -> List[str]:
    """Prune emprty if/else and try/except.

    >>> py_file = os.path.join(_PROJECT_ROOT, "src", "pytorch_lightning", "utilities", "cli.py")
    >>> import_path = ".".join(["pytorch_lightning", "utilities", "cli"])
    >>> with open(py_file, encoding="utf-8") as fp:
    ...     lines = [ln.rstrip() for ln in fp.readlines()]
    >>> lines = prune_imports_callables(lines)
    >>> lines = prune_empty_statements(lines)
    """
    kwords_pairs = ("with", "if ", "elif ", "else", "try", "except")
    body, tracking, skip_offset, last_count = [], False, 0, 0
    # todo: consider some more complex logic as for example only some leaves of if/else tree are empty
    for i, ln in enumerate(lines):
        offset = len(ln) - len(ln.lstrip())
        # skipp all decorators
        if ln.lstrip().startswith("@"):
            # consider also multi-line decorators
            if "(" in ln and ")" not in ln:
                tracking, skip_offset = True, offset
            continue
        # in case of mating the class args are multi-line
        if tracking and ln and offset <= skip_offset and not any(ln.lstrip().startswith(c) for c in ")]}"):
            tracking = False
        starts = [k for k in kwords_pairs if ln.lstrip().startswith(k)]
        if starts:
            start, count = starts[0], -1
            # look forward if this statement has a body
            for ln_ in lines[i:]:
                offset_ = len(ln_) - len(ln_.lstrip())
                if count == -1 and ln_.rstrip().endswith(":"):
                    count = 0
                elif ln_ and offset_ <= offset:
                    break
                # skipp all til end of statement
                elif ln_.lstrip():
                    # count non-zero body lines
                    count += 1
            # cache the last key body as the supplement canot be without
            if start in ("if", "elif", "try"):
                last_count = count
            if count <= 0 or (start in ("else", "except") and last_count <= 0):
                tracking, skip_offset = True, offset
        if not tracking:
            body.append(ln)
    return body


def prune_comments_docstrings(lines: List[str]) -> List[str]:
    """Prune all doctsrings with triple " notation.

    >>> py_file = os.path.join(_PROJECT_ROOT, "src", "pytorch_lightning", "loggers", "csv_logs.py")
    >>> import_path = ".".join(["pytorch_lightning", "loggers", "csv_logs"])
    >>> with open(py_file, encoding="utf-8") as fp:
    ...     lines = [ln.rstrip() for ln in fp.readlines()]
    >>> lines = prune_comments_docstrings(lines)
    """
    body, tracking = [], False
    for ln in lines:
        if "#" in ln and "noqa:" not in ln:
            ln = ln[: ln.index("#")]
        if not tracking and any(ln.lstrip().startswith(s) for s in ['"""', 'r"""']):
            # oneliners skip directly
            if len(ln.strip()) >= 6 and ln.rstrip().endswith('"""'):
                continue
            tracking = True
        elif ln.rstrip().endswith('"""'):
            tracking = False
            continue
        if not tracking:
            body.append(ln.rstrip())
    return body


def create_meta_package(src_folder: str, pkg_name: str = "pytorch_lightning", lit_name: str = "pytorch"):
    """Parse the real python package and for each module create a mirroe version with repalcing all function and
    class implementations by cross-imports to the true package.

    As validation run in termnal: `flake8 src/lightning/ --ignore E402,F401,E501`

    >>> create_meta_package(os.path.join(_PROJECT_ROOT, "src"))
    """
    package_dir = os.path.join(src_folder, pkg_name)
    # shutil.rmtree(os.path.join(src_folder, "lightning", lit_name))
    py_files = glob.glob(os.path.join(src_folder, pkg_name, "**", "*.py"), recursive=True)
    for py_file in py_files:
        local_path = py_file.replace(package_dir + os.path.sep, "")
        fname = os.path.basename(py_file)
        if "-" in local_path:
            continue
        with open(py_file, encoding="utf-8") as fp:
            lines = [ln.rstrip() for ln in fp.readlines()]
        import_path = pkg_name + "." + local_path.replace(".py", "").replace(os.path.sep, ".")
        import_path = import_path.replace(".__init__", "")

        if fname in ("__about__.py", "__version__.py"):
            body = lines
        else:
            if fname.startswith("_") and fname not in ("__init__.py", "__main__.py"):
                logging.warning(f"unsupported file: {local_path}")
                continue
            # ToDO: perform some smarter parsing - preserve Constants, lambdas, etc
            body = prune_comments_docstrings(lines)
            if fname not in ("__init__.py", "__main__.py"):
                body = prune_imports_callables(body)
            body = replace_block_with_imports([ln.rstrip() for ln in body], import_path, "class")
            body = replace_block_with_imports(body, import_path, "def")
            body = replace_block_with_imports(body, import_path, "async def")
            body = replace_vars_with_imports(body, import_path)
            body_len = -1
            # in case of several in-depth statements
            while body_len != len(body):
                body_len = len(body)
                body = prune_empty_statements(body)
            # TODO: add try/catch wrapper for whole body,
            #  so when import fails it tells you what is the package version this meta package was generated for...

        # todo: apply pre-commit formatting
        body = [ln for ln, _group in groupby(body)]
        lines = []
        # drop duplicated lines
        for ln in body:
            if ln + os.linesep not in lines or ln in (")", ""):
                lines.append(ln + os.linesep)
        # compose the target file name
        new_file = os.path.join(src_folder, "lightning", lit_name, local_path)
        os.makedirs(os.path.dirname(new_file), exist_ok=True)
        with open(new_file, "w", encoding="utf-8") as fp:
            fp.writelines(lines)


def _download_frontend(root: str = _PROJECT_ROOT):
    """Downloads an archive file for a specific release of the Lightning frontend and extracts it to the correct
    directory."""

    try:
        build_dir = "build"
        frontend_dir = pathlib.Path(root, "src", "lightning_app", "ui")
        download_dir = tempfile.mkdtemp()

        shutil.rmtree(frontend_dir, ignore_errors=True)
        response = urllib.request.urlopen(_LIGHTNING_FRONTEND_RELEASE_URL)

        file = tarfile.open(fileobj=response, mode="r|gz")
        file.extractall(path=download_dir)

        shutil.move(os.path.join(download_dir, build_dir), frontend_dir)
        print("The Lightning UI has successfully been downloaded!")

    # If installing from source without internet connection, we don't want to break the installation
    except Exception:
        print("The Lightning UI downloading has failed!")
