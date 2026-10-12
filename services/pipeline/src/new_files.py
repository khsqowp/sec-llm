"""Create-only UTF-8 outputs; never truncate an existing file."""
from contextlib import ExitStack
from pathlib import Path
from typing import Callable, List


def write_new_files(paths: List[Path], render: Callable[[List[Path]], List[str]]) -> List[Path]:
    """Reserve a whole file group exclusively; render links using actual names."""
    if len(set(paths)) != len(paths):
        raise ValueError("Output paths must be distinct")
    for path in paths:
        path.parent.mkdir(parents=True, exist_ok=True)
    counter = 0
    while True:
        candidates = [p if counter == 0 else p.with_name(f"{p.stem}_{counter}{p.suffix}") for p in paths]
        reserved = []
        try:
            with ExitStack() as stack:
                outputs = []
                for candidate in candidates:
                    outputs.append(stack.enter_context(candidate.open("x", encoding="utf-8")))
                    reserved.append(candidate)
                contents = render(candidates)
                if len(contents) != len(outputs):
                    raise ValueError("Output count differs from reserved file count")
                for output, content in zip(outputs, contents):
                    output.write(content)
            return candidates
        except FileExistsError:
            # Remove only files exclusively created by this failed reservation.
            for candidate in reserved:
                candidate.unlink()
            counter += 1
        except Exception:
            for candidate in reserved:
                candidate.unlink()
            raise


def write_new_file(path: Path, content: str) -> Path:
    return write_new_files([path], lambda _: [content])[0]
