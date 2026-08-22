import uuid
from typing import Any
from contextmesh.config import TrackerConfig
from contextmesh.models.nodes import ContextResponse

class SavingsTracker:
    def __init__(self, db: Any, config: TrackerConfig):
        self.db = db
        self.config = config

    async def record_turn(self, session_id: str, task_id: str | None, response: ContextResponse) -> dict:
        accumulated = await self.db.get_cumulative_tokens(session_id)
        turn_id = f"turn_{uuid.uuid4().hex[:12]}"
        
        await self.db.record_turn_savings(
            turn_id=turn_id,
            session_id=session_id,
            task_id=task_id,
            accumulated_tokens=accumulated,
            routed_tokens=response.total_tokens,
            mcp_overhead_tokens=self.config.mcp_call_overhead_tokens,
            hot_tokens=response.hot_tokens,
            warm_tokens=response.warm_tokens,
            cold_tokens=response.cold_tokens,
            repo_tokens=response.repo_tokens,
            input_price_per_mtok=self.config.input_price_per_mtok,
            included_nodes=response.included_node_ids
        )
        
        return {
            "turn_id": turn_id,
            "accumulated": accumulated,
            "routed": response.total_tokens,
            "saved": max(0, accumulated - response.total_tokens),
            "net_saved": max(0, accumulated - response.total_tokens - self.config.mcp_call_overhead_tokens)
        }

    async def get_session_summary(self, session_id: str) -> dict:
        rows = await self.db.fetchall("SELECT * FROM token_savings WHERE session_id = ?", (session_id,))
        if not rows:
            return {
                'session_id': session_id,
                'total_turns': 0, 'total_accumulated_tokens': 0, 'total_routed_tokens': 0,
                'total_tokens_saved': 0, 'total_net_tokens_saved': 0, 'avg_compression_ratio': 1.0,
                'total_cost_saved_usd': 0.0, 'best_turn_savings': 0
            }
            
        tot_accum = sum(r['accumulated_session_tokens'] for r in rows)
        tot_routed = sum(r['routed_tokens'] for r in rows)
        tot_saved = sum(r['tokens_saved'] for r in rows)
        tot_net = sum(r['net_tokens_saved'] for r in rows)
        cost_saved = sum(r['cost_saved_usd'] for r in rows)
        best_save = max((r['tokens_saved'] for r in rows), default=0)
        
        return {
            'session_id': session_id,
            'total_turns': len(rows),
            'total_accumulated_tokens': tot_accum,
            'total_routed_tokens': tot_routed,
            'total_tokens_saved': tot_saved,
            'total_net_tokens_saved': tot_net,
            'avg_compression_ratio': (tot_routed / tot_accum) if tot_accum > 0 else 1.0,
            'total_cost_saved_usd': cost_saved,
            'best_turn_savings': best_save
        }

    async def get_global_summary(self) -> dict:
        rows = await self.db.fetchall("SELECT * FROM token_savings")
        sessions = set(r['session_id'] for r in rows)
        if not rows:
            return {
                'session_count': 0, 'total_turns': 0, 'total_accumulated_tokens': 0, 
                'total_routed_tokens': 0, 'total_tokens_saved': 0, 'total_net_tokens_saved': 0, 
                'avg_compression_ratio': 1.0, 'total_cost_saved_usd': 0.0, 'best_turn_savings': 0
            }
            
        tot_accum = sum(r['accumulated_session_tokens'] for r in rows)
        tot_routed = sum(r['routed_tokens'] for r in rows)
        tot_saved = sum(r['tokens_saved'] for r in rows)
        tot_net = sum(r['net_tokens_saved'] for r in rows)
        cost_saved = sum(r['cost_saved_usd'] for r in rows)
        best_save = max((r['tokens_saved'] for r in rows), default=0)
        
        return {
            'session_count': len(sessions),
            'total_turns': len(rows),
            'total_accumulated_tokens': tot_accum,
            'total_routed_tokens': tot_routed,
            'total_tokens_saved': tot_saved,
            'total_net_tokens_saved': tot_net,
            'avg_compression_ratio': (tot_routed / tot_accum) if tot_accum > 0 else 1.0,
            'total_cost_saved_usd': cost_saved,
            'best_turn_savings': best_save
        }

    async def get_recent_turns(self, session_id: str, limit: int = 20) -> list[dict]:
        return await self.db.fetchall(
            "SELECT * FROM token_savings WHERE session_id = ? ORDER BY timestamp DESC LIMIT ?", 
            (session_id, limit)
        )

_tracker: SavingsTracker | None = None

def init_tracker(db: Any, config: TrackerConfig) -> SavingsTracker:
    global _tracker
    _tracker = SavingsTracker(db, config)
    return _tracker

def get_tracker() -> SavingsTracker:
    global _tracker
    if _tracker is None:
        raise RuntimeError("SavingsTracker not initialized")
    return _tracker
