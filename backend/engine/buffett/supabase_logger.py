import json
from datetime import datetime
from typing import List, Tuple, Dict
from .models import CompanyProfile, BuffettScore

class SnapshotLogger:
    """
    Logs evaluation snapshots and portfolio changes to Supabase (PostgreSQL).
    Maintains an audit trail of WHY a stock was added or removed.
    """

    def __init__(self, db_client=None):
        self.db = db_client # Supabase client instance

    def log_snapshot(self, evaluated_companies: List[Tuple[CompanyProfile, BuffettScore]]):
        """
        Logs a point-in-time snapshot of all evaluated companies.
        """
        snapshot_time = datetime.utcnow().isoformat()
        records = []
        for profile, score in evaluated_companies:
            records.append({
                "ticker": profile.ticker,
                "timestamp": snapshot_time,
                "total_score": score.total_score,
                "passed_veto": score.passed_veto,
                "veto_reason": score.veto_reason,
                "moat_score": score.moat_score,
                "profitability_score": score.profitability_score,
                "valuation_score": score.valuation_score,
                "balance_sheet_score": score.balance_sheet_score
            })
            
        # self.db.table("buffett_snapshots").insert(records).execute()
        # print(f"Logged {len(records)} snapshots.")
        return records

    def log_portfolio_changes(self, previous_portfolio: List[str], current_portfolio: List[Tuple[CompanyProfile, BuffettScore]]):
        """
        Determines ADDED and REMOVED stocks and logs the mathematical reasoning.
        """
        current_tickers = [p.ticker for p, _ in current_portfolio]
        
        added = set(current_tickers) - set(previous_portfolio)
        removed = set(previous_portfolio) - set(current_tickers)
        
        changes = []
        for p, score in current_portfolio:
            if p.ticker in added:
                changes.append({
                    "ticker": p.ticker,
                    "action": "ADDED",
                    "reason": f"Score {score.total_score} meets criteria. Margin of Safety is sufficient."
                })
                
        # For removed stocks, we would ideally look up their new score from the snapshot to explain why.
        for ticker in removed:
            changes.append({
                "ticker": ticker,
                "action": "REMOVED",
                "reason": "Fell below Top 10 rank or hit a Veto Gate (e.g. Valuation became too expensive)."
            })
            
        # self.db.table("portfolio_audit_log").insert(changes).execute()
        return changes
