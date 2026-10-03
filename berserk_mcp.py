"""Launcher kept so `python3 berserk_mcp.py` still starts the server.

The code lives in the berserk_mcp/ package. A package wins over a module of
the same name on import, so this file only runs as a script.
"""

from berserk_mcp import main

if __name__ == "__main__":
    main()
