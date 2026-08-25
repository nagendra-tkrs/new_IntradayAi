"""
Phase 10: Strategy Rollback / Version History

Tracks performance of each strategy version in production.
Detects performance degradation and provides rollback capability.
"""
import json
import os
from datetime import datetime
from typing import Optional, Dict, List
from app.services.strategy_config import StrategyVersion, STRATEGY_REGISTRY, get_strategy
from app.core.market_session import now_ist


class StrategyVersionHistory:
    """
    Tracks deployed strategy versions, their performance over time,
    and provides automatic rollback when degradation is detected.
    """
    
    def __init__(self, history_file: str = None):
        self.history_file = history_file or os.path.join(
            os.path.dirname(__file__), '..', 'data', 'strategy_history.json'
        )
        os.makedirs(os.path.dirname(self.history_file), exist_ok=True)
        self.history = self._load_history()
    
    def _load_history(self) -> dict:
        """Load version deployment history."""
        if os.path.exists(self.history_file):
            with open(self.history_file, 'r') as f:
                return json.load(f)
        return {
            'deployments': [],
            'performance_snapshots': [],
            'rollbacks': [],
        }
    
    def _save_history(self):
        """Save history to disk."""
        with open(self.history_file, 'w') as f:
            json.dump(self.history, f, indent=2, default=str)
    
    def deploy_strategy(self, version_name: str, performance_metrics: dict = None):
        """
        Record a strategy deployment to production.
        
        Args:
            version_name: Strategy version to deploy (e.g., 'v1')
            performance_metrics: Optional initial performance snapshot
        """
        if version_name not in STRATEGY_REGISTRY:
            raise ValueError(f"Strategy version '{version_name}' not in registry")
        
        deployment = {
            'version': version_name,
            'deployed_at': now_ist().isoformat(),
            'deployed_by': 'daily_improvement',
        }
        
        self.history['deployments'].append(deployment)
        
        if performance_metrics:
            snapshot = {
                'version': version_name,
                'timestamp': now_ist().isoformat(),
                **performance_metrics,
            }
            self.history['performance_snapshots'].append(snapshot)
        
        self._save_history()
        print(f"  Deployed strategy: {version_name}")
    
    def record_performance(self, version_name: str, metrics: dict):
        """Record a performance snapshot for a deployed strategy version."""
        snapshot = {
            'version': version_name,
            'timestamp': now_ist().isoformat(),
            **metrics,
        }
        self.history['performance_snapshots'].append(snapshot)
        self._save_history()
    
    def get_current_deployed(self) -> Optional[str]:
        """Get the currently deployed strategy version."""
        if not self.history['deployments']:
            return None
        return self.history['deployments'][-1]['version']
    
    def get_deployment(self, version_name: str) -> Optional[dict]:
        """Get deployment record for a specific version."""
        for d in reversed(self.history['deployments']):
            if d['version'] == version_name:
                return d
        return None
    
    def get_recent_performance(self, version_name: str, lookback: int = 5) -> List[dict]:
        """Get recent performance snapshots for a version."""
        snapshots = [
            s for s in self.history['performance_snapshots']
            if s['version'] == version_name
        ]
        return snapshots[-lookback:]
    
    def detect_degradation(self, version_name: str, 
                           metric: str = 'avg_profit_factor',
                           threshold: float = 0.3) -> bool:
        """
        Detect if a strategy version is degrading.
        
        Args:
            version_name: Strategy version to check
            metric: Performance metric to monitor
            threshold: Degradation threshold (fraction of baseline)
        
        Returns:
            True if degradation detected, False otherwise
        """
        snapshots = self.get_recent_performance(version_name, lookback=5)
        
        if len(snapshots) < 3:
            return False
        
        # Get baseline (first snapshot) and recent average (last 2)
        baseline = snapshots[0].get(metric, 0)
        recent = snapshots[-2:]
        recent_avg = sum(s.get(metric, 0) for s in recent) / len(recent)
        
        if baseline == 0:
            if recent_avg < 0:
                return True
            return False
        
        ratio = recent_avg / baseline
        
        if ratio < threshold:
            print(f"  DEGRADATION DETECTED for {version_name}: "
                  f"baseline={baseline:.2f}, recent={recent_avg:.2f}, ratio={ratio:.2f}")
            return True
        
        return False
    
    def rollback(self, reason: str = "Performance degradation detected"):
        """
        Rollback to the previous strategy version.
        Also updates the daily improvement state file.
        
        Returns:
            The version rolled back to, or None if no previous version exists
        """
        deployments = self.history['deployments']
        
        if len(deployments) < 2:
            print(f"  No previous version to rollback to")
            return None
        
        current = deployments[-1]['version']
        previous = deployments[-2]['version']
        
        rollback_entry = {
            'from_version': current,
            'to_version': previous,
            'reason': reason,
            'rolled_back_at': now_ist().isoformat(),
            'deployed_by': 'auto_rollback',
        }
        
        self.history['rollbacks'].append(rollback_entry)
        self._save_history()
        
        # Update the daily improvement state to reflect rollback
        state_file = os.path.join(
            os.path.dirname(__file__), '..', 'data', 'daily_improvement', 'daily_state.json'
        )
        if os.path.exists(state_file):
            with open(state_file, 'r') as f:
                state = json.load(f)
            state['current_strategy'] = previous
            state['last_update'] = now_ist().isoformat()
            state['daily_stats'].append({
                'date': now_ist().date().isoformat(),
                'strategy': previous,
                'summary': {'rollbacks': True, 'reason': reason},
                'rollback_from': current,
            })
            with open(state_file, 'w') as f:
                json.dump(state, f, indent=2, default=str)
        
        print(f"  ROLLBACK: {current} -> {previous} ({reason})")
        return previous
    
    def get_rollback_history(self) -> List[dict]:
        """Get full rollback history."""
        return self.history['rollbacks']
    
    def get_all_deployments(self) -> List[dict]:
        """Get all deployment records."""
        return self.history['deployments']


class PerformanceMonitor:
    """
    Monitors live strategy performance and triggers rollback when needed.
    """
    
    def __init__(self, version_history: StrategyVersionHistory):
        self.version_history = version_history
        self.degradation_threshold = 0.5  # PF drops below 50% of baseline
    
    def check_and_rollback(self, current_version: str, 
                          current_metrics: dict) -> Optional[str]:
        """
        Check current performance and rollback if degraded.
        
        Returns:
            New version if rollback occurred, None if no action taken
        """
        self.version_history.record_performance(current_version, current_metrics)
        
        if self.version_history.detect_degradation(
            current_version, 
            metric='avg_profit_factor',
            threshold=self.degradation_threshold
        ):
            return self.version_history.rollback()
        
        return None
