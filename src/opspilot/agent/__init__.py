"""LangGraph investigation workflow (P10).

LangGraph pulls in the LangSmith client, which can send traces to a hosted
service when tracing is switched on in the environment. Nothing may leave the
machine (`rules.md` §2 zero cost and §11), so tracing is forced off here,
whatever the environment says.
"""

import os

for _flag in ("LANGSMITH_TRACING", "LANGCHAIN_TRACING_V2", "LANGCHAIN_TRACING"):
    os.environ[_flag] = "false"
