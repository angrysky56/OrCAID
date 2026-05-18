import asyncio

from openhands.workspace import DockerDevWorkspace


async def test():
    try:
        ws = DockerDevWorkspace(
            base_image="ubuntu:22.04", server_image=None, target="source-minimal"
        )
        with ws as w:
            res = w.execute_command("echo hello")
            print(res.stdout)
    except Exception as e:
        print("Error:", e)


asyncio.run(test())
