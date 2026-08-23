# Advanced Python workflows

These APIs are useful for services and integrations. Ordinary scripts can use
the synchronous API in [Python API](python-api.md).

## Cancel work in flight

```python
import lean_runtime as lean

environment = lean.setup(toolchain="v4.33.0")
job = environment.start_check("example : True := trivial")
cancelled = job.cancel()
result = job.result()
```

`cancel()` returns `False` once the job has completed. Cancellation is
cooperative and terminates the local child process.

## Use asyncio

```python
import asyncio
import lean_runtime as lean


async def main() -> None:
    environment = lean.setup(toolchain="v4.33.0")
    source = "example : True := trivial"
    result = await environment.check_async(source)
    results = await environment.check_many_async([source, source], concurrency=2)
    assert result.ok and all(item.ok for item in results)


asyncio.run(main())
```

Synchronous `setup()`, `check()`, and `Runtime.prepare()` also accept a
`threading.Event` through `cancel=`.

## Replay a capture

```python
import lean_runtime as lean

result = lean.replay("execution.capture.json")
```

The path must identify an execution capture created or supplied by the caller;
Lean Runtime does not bundle the placeholder file above.

## Keep a process alive

`spawn_interactive()` exposes line-buffered UTF-8 pipes for a line-oriented tool
provided by the selected environment:

```python
import lean_runtime as lean

environment = lean.setup(["mathlib@v4.33.0"])
with environment.spawn_interactive(["lake", "exe", "lean_bridge"]) as session:
    response = session.request_json({"id": 1, "method": "get_info", "params": {}})

result = session.close()
```

`lean_bridge` is an example project executable, not a program bundled with Lean
Runtime. `close()` is idempotent and terminates the process group when the tool
does not exit after its input closes.
