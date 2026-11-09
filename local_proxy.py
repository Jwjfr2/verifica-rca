"""Minimal HTTP CONNECT proxy (test fixture for --proxy plumbing)."""
import asyncio
import sys

CHUNK = 65536


async def pipe(reader, writer):
    try:
        while True:
            data = await reader.read(CHUNK)
            if not data:
                break
            writer.write(data)
            await writer.drain()
    except (ConnectionResetError, asyncio.IncompleteReadError):
        pass
    finally:
        try:
            writer.close()
        except Exception:
            pass


async def handle(reader, writer):
    try:
        head = await reader.readuntil(b"\r\n\r\n")
    except asyncio.IncompleteReadError:
        writer.close()
        return
    line = head.split(b"\r\n", 1)[0].decode("latin-1")
    parts = line.split()
    if len(parts) < 3 or parts[0] != "CONNECT":
        writer.write(b"HTTP/1.1 405 Method Not Allowed\r\n\r\n")
        await writer.drain()
        writer.close()
        return
    host, _, port = parts[1].rpartition(":")
    try:
        up_r, up_w = await asyncio.open_connection(host, int(port))
    except OSError:
        writer.write(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
        await writer.drain()
        writer.close()
        return
    writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
    await writer.drain()
    await asyncio.gather(pipe(reader, up_w), pipe(up_r, writer))


async def main(port):
    server = await asyncio.start_server(handle, "127.0.0.1", port)
    print(f"proxy listening on 127.0.0.1:{port}", flush=True)
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(main(int(sys.argv[1]) if len(sys.argv) > 1 else 8888))
