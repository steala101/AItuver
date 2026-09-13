"""配線が生きているかを、動かして確かめる。

**「実装がある」と「効いている」は別のこと。** Phase 4 の監査で、
`CognitiveState.irritation` が実装されてから一度も 0.0 以外を返して
いなかったことが分かった。値の階層が1つ違い、軸の名前も違っていた。
テストは通っていた——テストが実際の呼び出し側と違う形で値を渡していたので。

同じ壊れ方は、層が増えるたびに増える。だから**推測ではなく、その場で
一往復させて確かめる**仕組みを置く。
"""
from neuro_voice.diagnostics.wiring import (
    DiagnosticsReport, Probe, ProbeResult, ProbeStatus, run_all,
)

__all__ = [
    "DiagnosticsReport", "Probe", "ProbeResult", "ProbeStatus", "run_all",
]
