"""The local copy of a slow file: filling order, resuming, verifying, failing."""

import asyncio
import json
import os
import time

import pytest

from castomatic import cache as cache_mod
from castomatic.cache import BlockCache, CacheError
from castomatic.errors import UserError

BLOCK = 16


def _pattern(size):
    return bytes(i * 7 % 251 for i in range(size))


@pytest.fixture
def source(tmp_path):
    path = tmp_path / "Movie (2009).mp4"
    path.write_bytes(_pattern(BLOCK * 10 + 5))  # 11 blocks, the last one short
    return path


def opened(source, tmp_path, **kw):
    kw.setdefault("workers", 1)
    return BlockCache.open(source, tmp_path / "root", block_size=BLOCK, **kw)


def hold(cache, order=None):
    """Make the source read blocks only when told to, one per ``go`` event set."""
    go = asyncio.Semaphore(0)
    real = cache._read_source

    def read(fd, block):
        return real(fd, block)

    original = cache_mod._in_daemon_thread

    async def gated(func, *args):
        if func is read:
            await go.acquire()
            if order is not None:
                order.append(args[1])
        return await original(func, *args)

    cache._read_source = read
    return go, gated


def test_cache_root_and_folder(monkeypatch, tmp_path):
    monkeypatch.undo()
    monkeypatch.setattr(cache_mod.tempfile, "tempdir", str(tmp_path))
    assert cache_mod.default_cache_root() == tmp_path / f"castomatic-cache-{os.getuid()}" / "files"
    monkeypatch.undo()

    one = cache_mod.cache_dir(tmp_path, tmp_path / "The Movie (2009).mp4")
    assert one == cache_mod.cache_dir(tmp_path, tmp_path / "The Movie (2009).mp4")
    assert one != cache_mod.cache_dir(tmp_path, tmp_path / "other" / "The Movie (2009).mp4")
    assert one.name.startswith("The_Movie_2009-") and "(" not in one.name
    assert cache_mod.cache_dir(tmp_path, tmp_path / "???.mp4").name.startswith("video-")


async def test_fills_the_whole_file(source, tmp_path):
    cache = opened(source, tmp_path, workers=2)
    assert cache.blocks == 11 and not cache.complete and cache.have_bytes == 0
    seen = []
    await cache.wait_complete(lambda have, total, rate: seen.append(total))
    assert cache.complete and cache.have_bytes == cache.size and cache.fetched == cache.size
    assert cache.path.read_bytes() == source.read_bytes()
    await cache.stop()
    assert BlockCache.peek(source, tmp_path / "root") == "complete"
    assert BlockCache.peek_bytes(source, tmp_path / "root") == cache.size


async def test_a_complete_copy_is_reused_without_reading_the_source(source, tmp_path):
    cache = opened(source, tmp_path)
    await cache.wait_complete()
    await cache.stop()
    again = opened(source, tmp_path)
    assert again.complete
    again.start()
    assert not again._tasks and again.rate is None
    assert await again.read(3, 5) == source.read_bytes()[3:8]
    await again.stop()


async def test_a_different_size_starts_over(source, tmp_path):
    cache = opened(source, tmp_path)
    await cache.wait_complete()
    await cache.stop()
    with open(source, "ab") as handle:
        handle.write(b"more")
    assert BlockCache.peek(source, tmp_path / "root") is None
    assert BlockCache.peek_bytes(source, tmp_path / "root") == 0
    again = opened(source, tmp_path)
    assert again.have_blocks == 0 and again.size == source.stat().st_size
    await again.wait_complete()
    assert again.path.read_bytes() == source.read_bytes()
    await again.stop()


async def test_a_lost_data_file_or_bad_map_starts_over(source, tmp_path):
    cache = opened(source, tmp_path)
    await cache.wait_complete()
    await cache.stop()
    meta = cache.directory / cache_mod.META_NAME
    saved = json.loads(meta.read_text())

    meta.write_text(json.dumps({**saved, "have": [[0, 999]]}))
    assert opened(source, tmp_path).close() is None
    assert json.loads(meta.read_text())["have"] == []

    meta.write_text(json.dumps({**saved, "have": 5}))
    second = opened(source, tmp_path)
    assert second.have_blocks == 0
    second.close()

    meta.write_text("not json")
    third = opened(source, tmp_path)
    assert third.have_blocks == 0
    third.close()

    meta.write_text(json.dumps({**saved, "block_size": 99}))
    fourth = opened(source, tmp_path)
    assert fourth.have_blocks == 0
    fourth.close()


def test_partial_map_is_kept_and_peek_says_so(source, tmp_path):
    cache = opened(source, tmp_path)
    os.pwrite(cache._fd, source.read_bytes()[:BLOCK], 0)
    cache._have[0] = 1
    cache._have[1] = 1
    cache._have[4] = 1
    cache.close()
    meta = json.loads((cache.directory / cache_mod.META_NAME).read_text())
    assert meta["have"] == [[0, 2], [4, 5]] and meta["complete"] is False
    assert meta["source"] == str(source)
    assert BlockCache.peek(source, tmp_path / "root") == "partial"
    assert BlockCache.peek_bytes(source, tmp_path / "root") == 3 * BLOCK
    again = opened(source, tmp_path)
    assert again.have_blocks == 3
    again.close()


def test_peek_bytes_survives_an_odd_map(source, tmp_path, monkeypatch):
    monkeypatch.setattr(BlockCache, "_saved", staticmethod(lambda s, r: {"have": 3}))
    assert BlockCache.peek_bytes(source, tmp_path) == 0


def test_an_empty_source_or_unusable_folder_is_refused(tmp_path, source):
    empty = tmp_path / "empty.mp4"
    empty.write_bytes(b"")
    with pytest.raises(CacheError, match="is empty"):
        BlockCache.open(empty, tmp_path)
    blocker = tmp_path / "file"
    blocker.write_bytes(b"x")
    with pytest.raises(CacheError, match="cannot use the cache folder"):
        BlockCache.open(source, blocker)


def test_two_copies_of_castomatic_cannot_fill_one_cache(source, tmp_path):
    first = opened(source, tmp_path)
    with pytest.raises(CacheError, match="already using"):
        opened(source, tmp_path)
    first.close()
    opened(source, tmp_path).close()


def test_contiguous_and_has(source, tmp_path):
    cache = opened(source, tmp_path)
    cache._have[0:3] = b"\x01\x01\x01"
    cache._have[5] = 1
    assert cache.has(0, 3 * BLOCK) and cache.has(cache.size, 1)
    assert not cache.has(0, 3 * BLOCK + 1)
    assert cache.contiguous_from(0) == 3 * BLOCK
    assert cache.contiguous_from(BLOCK + 1) == 2 * BLOCK - 1
    assert cache.contiguous_from(3 * BLOCK) == 0
    assert cache.contiguous_from(cache.size) == 0
    assert cache.have_bytes == 4 * BLOCK
    cache._have[:] = b"\x01" * cache.blocks
    assert cache.contiguous_from(BLOCK) == cache.size - BLOCK
    assert cache.have_bytes == cache.size
    cache.close()


def test_next_block_order(source, tmp_path):
    cache = opened(source, tmp_path)
    assert cache._next_block() == 0
    cache.hint(5 * BLOCK)
    assert cache._next_block() == 5
    cache._inflight.add(5)
    assert cache._next_block() == 6
    cache._demand[2] = 1
    assert cache._next_block() == 2
    cache._have[2] = 1  # arrived while still wanted
    assert cache._next_block() == 6
    cache._have[6:] = b"\x01" * (cache.blocks - 6)
    assert cache._next_block() == 0  # past the end: back to what was skipped
    cache._have[:5] = b"\x01" * 5
    assert cache._next_block() is None  # the last one is on its way
    cache.hint(10**12)
    assert cache._cursor == cache.blocks - 1
    cache.hint(-5)
    assert cache._cursor == 0
    cache.close()


async def test_a_read_waits_and_jumps_the_queue(source, tmp_path, monkeypatch):
    cache = opened(source, tmp_path)
    order = []
    go, gated = hold(cache, order)
    monkeypatch.setattr(cache_mod, "_in_daemon_thread", gated)
    cache.start()
    go.release()
    await asyncio.sleep(0.05)
    reader = asyncio.ensure_future(cache.read(7 * BLOCK + 2, 3))
    await asyncio.sleep(0.05)
    assert not reader.done() and cache._cursor == 7
    for _ in range(40):
        go.release()
        if reader.done():
            break
        await asyncio.sleep(0.01)
    assert await reader == source.read_bytes()[7 * BLOCK + 2 : 7 * BLOCK + 5]
    # Blocks 0 and 1 were already claimed; then the demanded block, ahead of 2 to 6
    assert order[:3] == [0, 1, 7]
    for _ in range(40):
        go.release()
    await cache.wait_complete()
    assert sorted(order) == list(range(11)), "every block ends up here, whatever the viewing order"
    assert cache.path.read_bytes() == source.read_bytes()
    await cache.stop()


async def test_reading_what_is_here_moves_the_download_along(source, tmp_path):
    cache = opened(source, tmp_path)
    os.pwrite(cache._fd, source.read_bytes()[3 * BLOCK : 4 * BLOCK], 3 * BLOCK)
    cache._have[3] = 1
    assert await cache.read(3 * BLOCK, 2) == source.read_bytes()[3 * BLOCK : 3 * BLOCK + 2]
    assert cache._cursor == 3
    assert await cache.read(cache.size, 5) == b"" and await cache.read(0, 0) == b""
    cache.close()


async def test_read_past_the_end_is_trimmed(source, tmp_path):
    cache = opened(source, tmp_path)
    cache.start()
    assert await cache.read(cache.size - 2, 100) == source.read_bytes()[-2:]
    await cache.stop()


async def test_reader_for_other_threads(source, tmp_path):
    cache = opened(source, tmp_path)
    cache.start()
    read_at = cache.reader(asyncio.get_running_loop())
    data = await asyncio.get_running_loop().run_in_executor(None, read_at, 20, 6)
    assert data == source.read_bytes()[20:26]
    await cache.stop()


async def test_two_readers_of_one_block(source, tmp_path):
    cache = opened(source, tmp_path, workers=3)
    cache.start()
    first, second = await asyncio.gather(cache.read(4 * BLOCK, 4), cache.read(4 * BLOCK, 4))
    assert first == second == source.read_bytes()[4 * BLOCK : 4 * BLOCK + 4]
    assert not cache._demand
    await cache.stop()


async def test_a_long_wait_is_mentioned_once(source, tmp_path, monkeypatch):
    notes = []
    cache = opened(source, tmp_path, on_note=notes.append)
    monkeypatch.setattr(cache_mod, "SLOW_NOTE_AFTER", 0.01)
    reader = asyncio.ensure_future(cache.read(0, 1))  # nothing is fetching yet
    await asyncio.sleep(0.05)
    assert len(notes) == 1 and "waiting for the video" in notes[0]
    cache.start()
    assert await reader == source.read_bytes()[:1]
    await cache.stop()


async def test_a_dropped_drive_is_retried(source, tmp_path, monkeypatch):
    notes = []
    monkeypatch.setattr(cache_mod, "RETRY_DELAYS", (0, 0, 0, 0))
    cache = opened(source, tmp_path, on_note=notes.append)
    real = cache._read_source
    failures = iter([True, True, True])

    def flaky(fd, block):
        if next(failures, False):
            raise OSError("drive went away")
        return real(fd, block)

    cache._read_source = flaky
    await cache.wait_complete()
    assert cache.path.read_bytes() == source.read_bytes() and "not answering" in notes[0]
    await cache.stop()


async def test_a_drive_that_stays_gone_is_an_error(source, tmp_path, monkeypatch):
    monkeypatch.setattr(cache_mod, "RETRY_DELAYS", (0, 0))
    cache = opened(source, tmp_path)

    def broken(fd, block):
        raise OSError("gone")

    cache._read_source = broken
    reader = asyncio.ensure_future(cache.read(0, 1))
    cache.start()
    with pytest.raises(CacheError, match="gone") as caught:
        await reader
    assert isinstance(caught.value, UserError) and "drive" in caught.value.hint
    with pytest.raises(CacheError, match="gone"):
        await cache.wait_complete()
    await cache.stop()


async def test_a_source_that_shrank_is_an_error(source, tmp_path, monkeypatch):
    monkeypatch.setattr(cache_mod, "RETRY_DELAYS", ())
    cache = opened(source, tmp_path)
    source.write_bytes(b"x")
    cache.start()
    with pytest.raises(CacheError, match="ended early"):
        await cache.wait_complete()
    await cache.stop()


async def test_a_short_read_from_the_source_is_continued(source, tmp_path, monkeypatch):
    cache = opened(source, tmp_path)
    real = os.pread
    monkeypatch.setattr(cache_mod.os, "pread", lambda fd, n, at: real(fd, min(n, 3), at))
    fd = os.open(source, os.O_RDONLY)
    assert cache._read_source(fd, 1) == source.read_bytes()[BLOCK : 2 * BLOCK]
    os.close(fd)
    cache.close()


def test_close_quietly_ignores_a_bad_descriptor():
    read_end, write_end = os.pipe()
    os.close(write_end)
    os.close(read_end)
    cache_mod._close_quietly(read_end)
    time.sleep(0.02)


async def test_rate_and_progress_saves(source, tmp_path, monkeypatch):
    monkeypatch.setattr(cache_mod, "SAVE_INTERVAL", 0.01)
    cache = opened(source, tmp_path)
    assert cache.rate is None
    cache._started = time.monotonic() - 10
    cache._samples.append((time.monotonic() - 1e3, 1000))  # too old to count
    cache._samples.append((time.monotonic() - 1, 1000))
    assert cache.rate == pytest.approx(100.0, rel=0.01)
    cache._started = time.monotonic()
    assert cache.rate is None  # too soon to say
    # Saving happens while running
    cache.start()
    await cache.wait_complete()
    await asyncio.sleep(0.03)
    meta = json.loads((cache.directory / cache_mod.META_NAME).read_text())
    assert meta["complete"] is True
    await cache.stop()
    assert cache.rate is None


async def test_workers_wait_while_the_last_block_is_with_another(source, tmp_path, monkeypatch):
    cache = BlockCache(source, tmp_path / "r", source.stat().st_size, workers=1, block_size=BLOCK)
    cache.directory.mkdir()
    cache._open_data()
    cache._have[:] = b"\x01" * cache.blocks
    cache._have[0] = 0
    cache._inflight.add(0)
    real_wait = asyncio.wait_for

    async def quick(awaitable, timeout):
        return await real_wait(awaitable, 0.01)

    monkeypatch.setattr(cache_mod.asyncio, "wait_for", quick)
    cache._condition()
    worker = asyncio.ensure_future(cache._worker())
    await asyncio.sleep(0.03)
    assert not worker.done()
    cache._have[0] = 1
    await asyncio.wait_for(worker, 1)
    cache.close()


def test_saving_tolerates_a_full_disk(source, tmp_path, monkeypatch, caplog):
    cache = opened(source, tmp_path)

    def fail(fd):
        raise OSError("disk full")

    monkeypatch.setattr(cache_mod.os, "fsync", fail)
    cache._save()
    assert "could not save" in caplog.text
    monkeypatch.undo()
    cache.close()
    cache._save()  # closed: nothing to do


async def test_a_stuck_read_does_not_hold_up_stopping(source, tmp_path):
    cache = opened(source, tmp_path)
    release = __import__("threading").Event()

    def stuck(fd, block):
        release.wait(5)
        return b""

    cache._read_source = stuck
    cache.start()
    await asyncio.sleep(0.02)
    await asyncio.wait_for(cache.stop(), 1)
    release.set()


def _fake_disk(monkeypatch, root, free):
    """A disk with ``free`` bytes spare that deleting a folder of ``root`` gives 1 GB back to."""
    gained = lambda: sum(1 for _ in removed)  # noqa: E731
    removed = []
    real = cache_mod.shutil.rmtree

    def rmtree(path, ignore_errors=False):
        removed.append(path.name)
        real(path, ignore_errors=ignore_errors)

    monkeypatch.setattr(cache_mod.shutil, "rmtree", rmtree)
    usage = lambda path: type("Usage", (), {"free": free + gained() * 10**9})  # noqa: E731
    monkeypatch.setattr(cache_mod.shutil, "disk_usage", usage)
    return removed


def test_require_space(tmp_path, monkeypatch):
    _fake_disk(monkeypatch, tmp_path, 500 * 1024 * 1024)
    cache_mod.require_cache_space(tmp_path / "not" / "yet", 100)
    cache_mod.require_cache_space(tmp_path, 2 * 10**9, already=int(1.8 * 10**9))
    with pytest.raises(UserError, match="not enough free space"):
        cache_mod.require_cache_space(tmp_path / "absent", 2 * 10**9)


def _old_copy(root, name, age):
    folder = root / name
    folder.mkdir(parents=True)
    meta = folder / cache_mod.META_NAME
    meta.write_text("{}")
    os.utime(meta, (time.time() - age, time.time() - age))
    return folder


def test_old_copies_go_first_to_make_room(tmp_path, monkeypatch):
    oldest = _old_copy(tmp_path, "oldest", 300)
    older = _old_copy(tmp_path, "older", 200)
    newest = _old_copy(tmp_path, "newest", 100)
    keep = _old_copy(tmp_path, "keep", 999)
    bare = tmp_path / "bare"
    bare.mkdir()  # no map at all: the first to go
    (tmp_path / "stray.txt").write_text("not a copy")
    removed = _fake_disk(monkeypatch, tmp_path, 10**8)
    cache_mod.require_cache_space(tmp_path, int(1.5 * 10**9), keep=keep)
    assert removed == ["bare", "oldest"]
    assert older.exists() and newest.exists() and keep.exists() and not oldest.exists()


def test_a_copy_in_use_is_never_removed(source, tmp_path, monkeypatch):
    busy = opened(source, tmp_path)
    removed = _fake_disk(monkeypatch, tmp_path, 10**8)
    with pytest.raises(UserError, match="not enough"):
        cache_mod.require_cache_space(tmp_path / "root", 10**10)
    assert removed == [] and busy.directory.exists()
    busy.close()
    cache_mod.require_cache_space(tmp_path / "root", 8 * 10**8)
    assert removed == [busy.directory.name]
