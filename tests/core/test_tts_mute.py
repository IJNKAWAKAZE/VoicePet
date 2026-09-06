import asyncio
import threading

import pytest

from core.cancellation import CancellationSource
from core.tts import SynthesizedAudio, TtsPlaybackError, WindowsMciAudioPlayer

AUDIO = SynthesizedAudio(b"mp3", "audio/mpeg", ".mp3", "edge-tts")


def test_muted_player_discards_audio_and_unmute_allows_new_playback(tmp_path):
    async def scenario():
        commands = []

        def runner(command):
            commands.append(command)
            return "stopped" if command.startswith("status ") else ""

        player = WindowsMciAudioPlayer(command_runner=runner, temp_directory=tmp_path)
        try:
            await player.set_muted(True)
            await player.play(AUDIO, CancellationSource().token)
            assert commands == []
            assert list(tmp_path.iterdir()) == []
            await player.set_muted(False)
            await player.play(AUDIO, CancellationSource().token)
            assert sum(command.startswith("play ") for command in commands) == 1
            assert list(tmp_path.iterdir()) == []
        finally:
            player.close()

    asyncio.run(scenario())


def test_mute_stops_all_active_audio_before_returning(tmp_path):
    async def scenario():
        commands = []
        played = asyncio.Event()
        loop = asyncio.get_running_loop()

        def runner(command):
            commands.append(command)
            if sum(item.startswith("play ") for item in commands) == 2:
                loop.call_soon_threadsafe(played.set)
            return "playing" if command.startswith("status ") else ""

        player = WindowsMciAudioPlayer(
            command_runner=runner, temp_directory=tmp_path, poll_interval=0.01,
        )
        tasks = [asyncio.create_task(player.play(AUDIO, CancellationSource().token))
                 for _ in range(2)]
        try:
            await asyncio.wait_for(played.wait(), 1)
            await player.set_muted(True)
            aliases = {command.split()[1] for command in commands if command.startswith("play ")}
            assert {f"stop {alias}" for alias in aliases}.issubset(commands)
            await player.set_muted(False)
            await asyncio.wait_for(asyncio.gather(*tasks), 1)
            assert sum(command.startswith("play ") for command in commands) == 2
            assert list(tmp_path.iterdir()) == []
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            player.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("cancel_play", [False, True])
def test_mute_during_open_never_starts_old_audio_and_cleans_it(tmp_path, cancel_play):
    async def scenario():
        commands = []
        opened = asyncio.Event()
        release = threading.Event()
        loop = asyncio.get_running_loop()

        def runner(command):
            commands.append(command)
            if command.startswith("open "):
                loop.call_soon_threadsafe(opened.set)
                assert release.wait(2)
            return "stopped" if command.startswith("status ") else ""

        player = WindowsMciAudioPlayer(command_runner=runner, temp_directory=tmp_path)
        playback = asyncio.create_task(player.play(AUDIO, CancellationSource().token))
        mute = None
        try:
            await asyncio.wait_for(opened.wait(), 1)
            mute = asyncio.create_task(player.set_muted(True))
            await asyncio.sleep(0)
            assert not mute.done()
            if cancel_play:
                playback.cancel()
                await asyncio.sleep(0)
            release.set()
            await asyncio.wait_for(mute, 1)
            await player.set_muted(False)
            result = await asyncio.gather(playback, return_exceptions=True)
            if cancel_play:
                assert isinstance(result[0], asyncio.CancelledError)
            else:
                assert result == [None]
            assert not any(command.startswith("play ") for command in commands)
            alias = commands[0].rsplit(" ", 1)[1]
            assert f"close {alias}" in commands
            assert list(tmp_path.iterdir()) == []
        finally:
            release.set()
            playback.cancel()
            await asyncio.gather(playback, return_exceptions=True)
            if mute is not None:
                await asyncio.gather(mute, return_exceptions=True)
            player.close()

    asyncio.run(scenario())


def test_mute_reports_stop_failure_and_attempts_every_active_alias(tmp_path):
    async def scenario():
        commands = []
        played = asyncio.Event()
        loop = asyncio.get_running_loop()

        def runner(command):
            commands.append(command)
            if sum(item.startswith("play ") for item in commands) == 2:
                loop.call_soon_threadsafe(played.set)
            if command.startswith("stop "):
                raise RuntimeError("private device detail")
            if command.startswith("status "):
                return "stopped" if sum(item.startswith("play ") for item in commands) > 2 else "playing"
            return ""

        player = WindowsMciAudioPlayer(command_runner=runner, temp_directory=tmp_path)
        tasks = [asyncio.create_task(player.play(AUDIO, CancellationSource().token))
                 for _ in range(2)]
        try:
            await asyncio.wait_for(played.wait(), 1)
            with pytest.raises(TtsPlaybackError) as error:
                await player.set_muted(True)
            assert "private" not in str(error.value)
            assert sum(command.startswith("stop ") for command in commands) == 2
            await asyncio.wait_for(asyncio.gather(*tasks), 1)
            # 静音失败必须保留原有声音状态，与托盘的失败反馈保持一致
            await asyncio.wait_for(player.play(AUDIO, CancellationSource().token), 1)
            assert sum(command.startswith("play ") for command in commands) == 3
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            player.close()

    asyncio.run(scenario())
