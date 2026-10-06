"""Run the golden scenario and print the full structured result (incl. evidence + trace)."""
import json
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))
from cccp_agent import CommercialDecisionAgent, DecisionRequest, LiveCallSignal  # noqa: E402
from cccp_agent.adapters.synthetic import StubNarrator, SyntheticEstate  # noqa: E402

e = SyntheticEstate()
agent = CommercialDecisionAgent(e.customer_port(), e.interaction_port(), e.model_port(),
                                 e.guidance_port(), e.catalog, StubNarrator())
cid = sys.argv[1] if len(sys.argv) > 1 else "cust_001"
live = LiveCallSignal("call_demo", float(sys.argv[2])) if len(sys.argv) > 2 else None
print(json.dumps(agent.run(DecisionRequest(cid, date(2026, 10, 5), live_signal=live)).to_dict(), indent=2))
