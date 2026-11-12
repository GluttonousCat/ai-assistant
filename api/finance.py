"""Compatibility router aggregation; domain handlers live in dedicated modules."""
from fastapi import APIRouter
from api.agent_api import fin_router as agent_router, agent_stream_endpoint
from api.chains_api import (fin_router as chains_router, list_chains, get_chain_analysis,
                            forge_chain_stream_endpoint, save_chain_endpoint)
from api.schema_api import fin_router as schema_router, get_schema, finance_detect
from api.events import sse as _sse

fin_router = APIRouter()
fin_router.include_router(chains_router)
fin_router.include_router(agent_router)
fin_router.include_router(schema_router)
