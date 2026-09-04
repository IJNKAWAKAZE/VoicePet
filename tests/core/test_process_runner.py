import asyncio
import sys
import time
from pathlib import Path

import pytest

from core.cancellation import CancellationSource, CancelledError
from core.process_runner import (
    AsyncSubprocessRunner,
    ProcessConfigurationError,
)


def run_process(command_args, tmp_path, **overrides):
    source = overrides.pop("source", CancellationSource())
    values = {
        "program": Path(sys.executable),
        "args": command_args,
        "cwd": tmp_path,
        "timeout": 2.0,
        "output_limit": 1024,
        "token": source.token,
    }
    values.update(overrides)
    return AsyncSubprocessRunner().run(**values)


def test_process_runner_captures_exit_code_and_both_streams(tmp_path):
    outcome = asyncio.run(
        run_process(
            [
                "-c",
                "import sys; print('out'); print('err', file=sys.stderr); sys.exit(3)",
            ],
            tmp_path,
        )
    )

    assert outcome.exit_code == 3
    assert outcome.stdout.strip() == "out"
    assert outcome.stderr.strip() == "err"
    assert outcome.timed_out is False
    assert outcome.truncated is False


def test_process_runner_bounds_stdout_and_stderr_memory(tmp_path):
    outcome = asyncio.run(
        run_process(
            [
                "-c",
                "import sys; sys.stdout.write('a'*100); sys.stderr.write('b'*100)",
            ],
            tmp_path,
            output_limit=10,
        )
    )

    assert outcome.stdout == "a" * 10
    assert outcome.stderr == "b" * 10
    assert outcome.truncated is True


def test_process_runner_times_out_and_terminates_child(tmp_path):
    started = time.monotonic()
    outcome = asyncio.run(
        run_process(
            ["-c", "import time; time.sleep(5)"],
            tmp_path,
            timeout=0.05,
        )
    )

    assert outcome.timed_out is True
    assert time.monotonic() - started < 2


def test_process_runner_cancellation_terminates_child(tmp_path):
    async def scenario():
        source = CancellationSource()
        task = asyncio.create_task(
            run_process(
                ["-c", "import time; time.sleep(5)"],
                tmp_path,
                timeout=10,
                source=source,
            )
        )
        await asyncio.sleep(0.05)
        source.cancel("interrupt")

        with pytest.raises(CancelledError):
            await asyncio.wait_for(task, timeout=2)

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "overrides",
    [
        {"program": Path("relative.exe")},
        {"program": Path("C:/missing-private-program.exe")},
        {"args": [1]},
        {"timeout": 0},
        {"output_limit": 0},
    ],
)
def test_process_runner_rejects_invalid_requests_without_leaking_paths(
    tmp_path,
    overrides,
):
    with pytest.raises(ProcessConfigurationError) as captured:
        asyncio.run(run_process([], tmp_path, **overrides))
    assert "missing-private" not in str(captured.value)
