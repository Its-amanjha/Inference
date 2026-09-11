"""Independent official-SDK server used only by the local integration harness."""
import argparse
from pathlib import Path
from mcp.server.fastmcp import FastMCP

parser = argparse.ArgumentParser()
parser.add_argument('--port', type=int, required=True)
parser.add_argument('--counter', type=Path, required=True)
parser.add_argument('--json-response', action='store_true')
args = parser.parse_args()
server = FastMCP('Inference SDK integration', host='127.0.0.1', port=args.port,
                 json_response=args.json_response)


@server.tool()
def add(a: int, b: int) -> int:
    """Add two integers. The test records invocation count separately."""
    count = int(args.counter.read_text()) if args.counter.exists() else 0
    args.counter.write_text(str(count + 1))
    return a + b


if __name__ == '__main__':
    server.run(transport='streamable-http')
