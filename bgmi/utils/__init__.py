import functools
import glob
import gzip
import json
import logging
import os
import re
import struct
import subprocess
import sys
import tarfile
import time
import traceback
from io import BytesIO
from multiprocessing.pool import ThreadPool
from pathlib import Path
from shutil import move, rmtree
from typing import Any, Callable, List, Optional, Tuple, TypeVar

import requests
from anime_episode_parser import parse_episode as _parse_episode

from bgmi import __admin_version__, __version__
from bgmi.config import BGMI_PATH, IS_WINDOWS, cfg
from bgmi.lib.constants import SUPPORT_WEBSITE
from bgmi.website.model import Episode

F = TypeVar("F", bound=Callable[..., Any])

log_level = os.environ.get("BGMI_LOG") or "ERROR"
log_level = log_level.upper()
if log_level not in ["DEBUG", "INFO", "WARNING", "ERROR"]:
    print("log level error, doing nothing and exit")
    sys.exit(1)
logger = logging.getLogger("BGmi")
try:
    h = logging.FileHandler(cfg.log_path, "a+", "utf-8")
    handlers = [h]
    fs = logging.BASIC_FORMAT
    fmt = logging.Formatter(fs)
    h.setFormatter(fmt)
    logging.root.addHandler(h)
    logging.root.setLevel(log_level)
except OSError:
    logging.basicConfig(stream=sys.stdout, level=logging.getLevelName(log_level))


def log_utils_function(func: F) -> F:
    @functools.wraps(func)
    def echo_func(*func_args, **func_kwargs):  # type: ignore
        logger.debug("")
        logger.debug("start function %s %r %r", func.__name__, func_args, func_kwargs)
        r = func(*func_args, **func_kwargs)
        logger.debug("return function %s %r", func.__name__, r)
        logger.debug("")
        return r

    return echo_func  # type: ignore


if (
    IS_WINDOWS and "bash" not in os.getenv("SHELL", "").lower() and "zsh" not in os.getenv("SHELL", "").lower()
):  # pragma: no cover
    GREEN = ""
    YELLOW = ""
    RED = ""
    COLOR_END = ""
else:
    GREEN = "\033[1;32m"
    YELLOW = "\033[1;33m"
    RED = "\033[1;31m"
    COLOR_END = "\033[0m"

color_map = {
    "print_info": "",
    "print_success": GREEN,
    "print_warning": YELLOW,
    "print_error": RED,
}

indicator_map = {
    "print_info": "[*] ",
    "print_success": "[+] ",
    "print_warning": "[-] ",
    "print_error": "[x] ",
}

FRONTEND_NPM_URL = "https://registry.npmjs.com/bgmi-frontend/"
PACKAGE_JSON_URL = f"https://registry.npmjs.com/bgmi-frontend/{__admin_version__}"


def _indicator(f):  # type: ignore
    @functools.wraps(f)
    def wrapper(*args, **kwargs):  # type: ignore
        if kwargs.get("indicator", True):
            func_name = f.__qualname__
            args = (indicator_map.get(func_name, "") + args[0],)
        f(*args, **kwargs)
        sys.stdout.flush()

    return wrapper


def colorize(f):  # type: ignore
    @functools.wraps(f)
    def wrapper(*args, **kwargs):  # type: ignore
        func_name = f.__qualname__
        b, e = (
            color_map.get(func_name, ""),
            COLOR_END if color_map.get(func_name) else "",
        )
        args = tuple(b + s + e for s in args)
        return f(*args, **kwargs)

    return wrapper


@_indicator
@colorize
def print_info(message: str, indicator: bool = True) -> None:
    logger.info(message)
    print(message + "\n", end="")


@_indicator
@colorize
def print_success(message: str, indicator: bool = True) -> None:
    logger.info(message)
    print(message)


@_indicator
@colorize
def print_warning(message: str, indicator: bool = True) -> None:
    logger.warning(message)
    print(message)


@_indicator
@colorize
def print_error(message: str, exit_: bool = True, indicator: bool = True) -> None:
    logger.error(message)
    print(message)
    if exit_:
        sys.exit(1)


def print_version() -> str:
    return """BGmi {c}ver. {v}{e} built by {c}RicterZ{e} with ❤️

Github: https://github.com/BGmi/BGmi
Email: ricterzheng@gmail.com
Blog: https://ricterz.me""".format(
        c=YELLOW, v=__version__, e=COLOR_END
    )


@log_utils_function
def test_connection() -> bool:
    try:
        for website in SUPPORT_WEBSITE:
            if cfg.data_source == website["id"]:
                requests.request("head", website["url"], timeout=10)
    except requests.RequestException:
        return False
    return True


def bug_report() -> None:  # pragma: no cover
    print_error(
        "It seems that no bangumi found, if https://bangumi.moe can \n    be opened "
        "normally, please submit issue at: https://github.com/BGmi/BGmi/issues"
    )


_DEFAULT_TERMINAL_WIDTH = 80


@log_utils_function
def get_terminal_col() -> int:  # pragma: no cover
    # pylint: disable=import-outside-toplevel,import-error
    # https://gist.github.com/jtriley/1108174
    if not IS_WINDOWS:
        import fcntl
        import termios

        try:
            col = struct.unpack(
                "HHHH",
                fcntl.ioctl(0, termios.TIOCGWINSZ, struct.pack("HHHH", 0, 0, 0, 0)),
            )[
                1
            ]  # type: int

            return col
        except Exception:
            return _DEFAULT_TERMINAL_WIDTH
    else:
        try:
            from ctypes import create_string_buffer, windll  # type: ignore[attr-defined]

            # stdin handle is -10
            # stdout handle is -11
            # stderr handle is -12
            h = windll.kernel32.GetStdHandle(-12)
            csbi = create_string_buffer(22)
            res = windll.kernel32.GetConsoleScreenBufferInfo(h, csbi)
            if res:
                (
                    bufx,
                    bufy,
                    curx,
                    cury,
                    wattr,
                    left,
                    top,
                    right,
                    bottom,
                    maxx,
                    maxy,
                ) = struct.unpack("hhhhHhhhhhh", csbi.raw)
                sizex = right - left + 1  # type: int
                return sizex
            else:
                cols = int(subprocess.check_output("tput cols"))
                return cols
        except Exception:
            return _DEFAULT_TERMINAL_WIDTH


@log_utils_function
def check_update(mark: bool = True) -> None:
    def update() -> None:
        try:
            print_info("Checking update ...")
            pypi = requests.get("https://pypi.org/pypi/bgmi/json", timeout=60).json()
            version = pypi["info"]["version"]

            with open(os.path.join(BGMI_PATH, "latest"), "w", encoding="utf8") as f:
                f.write(version)

            if version > __version__:
                print_warning(
                    "Please update bgmi to the latest version {}{}{}."
                    "\nThen execute `bgmi upgrade` to migrate database".format(GREEN, version, COLOR_END)
                )
            else:
                print_success("Your BGmi is the latest version.")

            package_json = requests.get(PACKAGE_JSON_URL, timeout=60).json()
            admin_version = package_json["version"]
            if glob.glob(os.path.join(cfg.front_static_path, "package.json")):
                with open(os.path.join(cfg.front_static_path, "package.json"), encoding="utf8") as f:
                    local_version = json.loads(f.read())["version"]
                if [int(x) for x in admin_version.split(".")] > [int(x) for x in local_version.split(".")]:
                    get_web_admin(method="update")
            else:
                print_info("Use 'bgmi install' to install BGmi frontend / download delegate")
            if not mark:
                update()
                raise SystemExit
        except Exception as e:
            print_warning(f"Error occurs when checking update, {str(e)}")

    version_file = os.path.join(BGMI_PATH, "version")
    if not os.path.exists(version_file):
        with open(version_file, "w", encoding="utf8") as f:
            f.write(str(int(time.time())))
        update()

    with open(version_file, encoding="utf8") as f:
        try:
            data = int(f.read())
            if time.time() - 7 * 24 * 3600 > data:
                with open(version_file, "w", encoding="utf8") as f:
                    f.write(str(int(time.time())))
                update()
        except ValueError:
            pass


def parse_episode(episode_title: str) -> int:
    s, c = _parse_episode(episode_title)
    if c != 1:
        return 0

    return s or 0


def normalize_path(url: str) -> str:
    """
    normalize link to path

    :param url: path or url to normalize
    :type url: str
    :return: normalized path
    :rtype: str
    """
    url = url.replace("http://", "http/").replace("https://", "https/")
    illegal_char = [":", "*", "?", '"', "<", ">", "|", "'"]
    for char in illegal_char:
        url = url.replace(char, "")

    if url.startswith("/"):
        return url[1:]
    else:
        return url


def bangumi_save_path(bangumi_name: str) -> Path:
    name = normalize_path(bangumi_name)

    r = cfg.save_path_map.get(name)
    if r is None:
        return cfg.save_path.joinpath(name)

    if r.is_absolute():
        return r

    return cfg.save_path.joinpath(r)


def get_web_admin(method: str) -> None:
    print_info(f"{method[0].upper() + method[1:]}ing BGmi frontend")

    try:
        r = requests.get(FRONTEND_NPM_URL, timeout=60).json()
        version = requests.get(PACKAGE_JSON_URL, timeout=60).json()
        if "error" in version:  # pragma: no cover
            print(json.dumps(version, indent=2, ensure_ascii=False))
            print_error("unexpected npm error")
            return
        tar_url = r["versions"][version["version"]]["dist"]["tarball"]
        r = requests.get(tar_url, timeout=60)
    except requests.exceptions.ConnectionError:
        print_warning("failed to download web admin")
        return
    except json.JSONDecodeError:
        print_warning("failed to download web admin")
        return
    admin_zip = BytesIO(r.content)
    with gzip.GzipFile(fileobj=admin_zip) as f:
        tar_file = BytesIO(f.read())

    rmtree(cfg.front_static_path)
    os.makedirs(cfg.front_static_path)

    with tarfile.open(fileobj=tar_file) as tar_file_obj:
        tar_file_obj.extractall(path=cfg.front_static_path)

    for file in os.listdir(os.path.join(cfg.front_static_path, "package", "dist")):
        move(
            os.path.join(cfg.front_static_path, "package", "dist", file),
            os.path.join(cfg.front_static_path, file),
        )
    with open(os.path.join(cfg.front_static_path, "package.json"), "w+", encoding="utf8") as pkg:
        pkg.write(json.dumps(version))
    print_success("Web admin page {} successfully. version: {}".format(method, version["version"]))


@log_utils_function
def convert_cover_url_to_path(cover_url: str) -> Tuple[str, str]:
    """
    convert bangumi cover to file path

    :param cover_url: bangumi cover path
    :type cover_url:str
    :rtype: str,str
    :return: dir_path, file_path
    """

    cover_url = normalize_path(cover_url)
    file_path = os.path.join(cfg.save_path, "cover")
    file_path = os.path.join(file_path, cover_url)
    dir_path = os.path.dirname(file_path)

    return dir_path, file_path


@log_utils_function
def download_file(url: str) -> Optional[requests.Response]:
    if url.startswith("https://") or url.startswith("http://"):
        print_info(f"Download: {url}")
        return requests.get(url, timeout=60)
    return None


def download_cover(cover_url_list: List[str]) -> None:
    p = ThreadPool(4)
    content_list = p.map(download_file, cover_url_list)
    p.close()
    for index, r in enumerate(content_list):
        if not r:
            continue

        dir_path, file_path = convert_cover_url_to_path(cover_url_list[index])
        if not glob.glob(dir_path):
            os.makedirs(dir_path)
        with open(file_path, "wb") as f:
            f.write(r.content)


def episode_filter_regex(data: List[Episode], regex: Optional[str] = None) -> List[Episode]:
    """

    :param data: list of bangumi dict
    :param regex: regex
    """
    if regex:
        try:
            match = re.compile(regex)
            data = [s for s in data if match.findall(s.title)]
        except re.error as e:
            if os.getenv("DEBUG"):  # pragma: no cover
                traceback.print_exc()
                raise e
            print_warning(f"can't compile regex {regex}, skipping filter by regex")

    if cfg.enable_global_filters:
        exclude_keywords = [t.strip().lower() for t in cfg.global_filters]
        data = [x for x in data if not x.contains_any_words(exclude_keywords)]

    return data
