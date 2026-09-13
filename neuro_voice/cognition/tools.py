"""道具の台帳。**何ができて、何が戻せないかを、コード側が持つ。**

Phase 6 までツールは文字列だった——`tool == "web_search"` の比較が
呼び出し側に散らばっていて、「このツールは副作用があるか」を
**誰も宣言していなかった**。新しいツールを足すたびに、各呼び出し側が
自前で条件を書き足すことになる。書き忘れた側から漏れる。

ここが唯一の宣言場所になる。守りたいのは3つ:

* **LLM の説明文で副作用を決めない。** 「これは安全な操作です」と
  モデルが書けば安全になる、という仕組みにしない。
* **知らないツールは実行しない。** 台帳に無いものは拒否であって、
  「たぶん読み取りだろう」ではない。
* **機密パラメータを記録しない。** `sensitive=True` の値は
  ログにも DB にも出さない（第12条）。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from neuro_voice.cognition.goals import ActionCategory

# ---------------------------------------------------------------------------
# 副作用の分類
# ---------------------------------------------------------------------------


class EffectCategory(StrEnum):
    """そのツールが世界に何を残すか。**取り消せるかどうかで分けてある。**"""

    #: 自分の中だけ。記憶検索・状態整理。
    INTERNAL = "internal"
    #: 外を読むだけ。何も残さない。
    READ_ONLY = "read_only"
    #: 手元に書くが、元へ戻せる。
    LOCAL_REVERSIBLE = "local_reversible"
    #: 外部サービスへ書く。**戻せるとは限らない。**
    EXTERNAL_WRITE = "external_write"
    #: 人へ届く。**届いたら取り消せない。**
    PERSON_DIRECTED = "person_directed"
    #: 消す・買う・操作する。**取り返しがつかない。**
    DESTRUCTIVE = "destructive"


#: 副作用の分類 → 既存の `ActionCategory`。
#:
#: **既存の `decide_permission()` を作り直さない。** Phase 6 で作った
#: 7分類の判定はそのまま使い、ツール側の語彙をそこへ写すだけにする。
#: 判定の場所が2つあると、片方だけ直した時に必ず食い違う。
EFFECT_TO_ACTION_CATEGORY: dict[str, ActionCategory] = {
    EffectCategory.INTERNAL: ActionCategory.INTERNAL,
    EffectCategory.READ_ONLY: ActionCategory.EXTERNAL_READ,
    # **戻せることと、勝手にやってよいことは別。**
    # 「元へ戻せるから確認は要らない」にすると、戻し方を知っているのは
    # こちらだけで、ユーザーは何が起きたか分からないまま残る。
    EffectCategory.LOCAL_REVERSIBLE: ActionCategory.EXTERNAL_WRITE,
    EffectCategory.EXTERNAL_WRITE: ActionCategory.EXTERNAL_WRITE,
    EffectCategory.PERSON_DIRECTED: ActionCategory.PERSON_DIRECTED,
    EffectCategory.DESTRUCTIVE: ActionCategory.DESTRUCTIVE,
}

#: 副作用が無いと言い切れる分類。**自動 Retry を許すのはここだけ。**
NO_SIDE_EFFECT = frozenset({EffectCategory.INTERNAL, EffectCategory.READ_ONLY})
#: 結果が不明な時に、勝手にやり直してはいけない分類。
IRREVERSIBLE = frozenset({
    EffectCategory.EXTERNAL_WRITE, EffectCategory.PERSON_DIRECTED,
    EffectCategory.DESTRUCTIVE,
})
#: 誰に対してやるのかを確かめる必要がある分類。
NEEDS_TARGET = frozenset({
    EffectCategory.PERSON_DIRECTED, EffectCategory.DESTRUCTIVE,
})


def effect_of(value: Any) -> EffectCategory | None:
    """文字列から分類へ。**知らない文字列は `None`**（分からないと言う）。"""
    try:
        return EffectCategory(str(value))
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# パラメータ
# ---------------------------------------------------------------------------

_TYPES: dict[str, tuple[type, ...]] = {
    "str": (str,), "int": (int,), "float": (int, float), "bool": (bool,),
}


@dataclass(frozen=True, slots=True)
class ParameterSpec:
    """引数1つの決まり。**ツールへ渡してよいのはここに書いたものだけ。**"""

    name: str
    kind: str = "str"
    required: bool = True
    max_length: int = 400
    choices: tuple[str, ...] = ()
    minimum: float | None = None
    maximum: float | None = None
    #: 記録してはいけない値。**ログにも DB にも出さない**（第12条・第30項）。
    sensitive: bool = False
    #: この引数が操作対象を指すか。`DESTRUCTIVE` では必須。
    is_target: bool = False


class SchemaError(StrEnum):
    MISSING = "missing_required_parameter"
    UNKNOWN = "unknown_parameter"
    TYPE = "wrong_type"
    RANGE = "out_of_range"
    LENGTH = "too_long"
    CHOICE = "not_in_choices"


@dataclass(frozen=True, slots=True)
class NormalisedParameters:
    values: dict[str, Any] = field(default_factory=dict)
    errors: tuple[str, ...] = ()
    #: 記録してよい形（`sensitive` を伏せたもの）。
    loggable: dict[str, Any] = field(default_factory=dict)
    target_ids: tuple[str, ...] = ()

    @property
    def valid(self) -> bool:
        return not self.errors

    @property
    def missing_only(self) -> bool:
        """**足りないだけ**なら聞き直せる。型違いは聞き直しても直らない。"""
        return bool(self.errors) and all(
            item.startswith(str(SchemaError.MISSING)) for item in self.errors)


def normalise_parameters(
    specs: tuple[ParameterSpec, ...], raw: dict[str, Any] | None,
) -> NormalisedParameters:
    """引数を決まった形へ。**知らない引数は落とすのではなく、拒む。**

    黙って落とすと「渡したつもり」で通ってしまう。宛先を1つ間違えた
    ような時に、それが一番危ない。
    """
    given = dict(raw or {})
    known = {spec.name: spec for spec in specs}
    values: dict[str, Any] = {}
    loggable: dict[str, Any] = {}
    targets: list[str] = []
    errors: list[str] = []

    for name in given:
        if name not in known:
            errors.append(f"{SchemaError.UNKNOWN}:{name}")

    for spec in specs:
        if spec.name not in given or given[spec.name] is None:
            if spec.required:
                errors.append(f"{SchemaError.MISSING}:{spec.name}")
            continue
        value = given[spec.name]
        expected = _TYPES.get(spec.kind, (str,))
        # bool は int の派生。数値として受け取らない。
        if spec.kind != "bool" and isinstance(value, bool):
            errors.append(f"{SchemaError.TYPE}:{spec.name}")
            continue
        if not isinstance(value, expected):
            errors.append(f"{SchemaError.TYPE}:{spec.name}")
            continue
        if spec.kind == "str":
            text = str(value).strip()
            if len(text) > spec.max_length:
                errors.append(f"{SchemaError.LENGTH}:{spec.name}")
                continue
            if spec.choices and text not in spec.choices:
                errors.append(f"{SchemaError.CHOICE}:{spec.name}")
                continue
            value = text
        elif spec.kind in {"int", "float"}:
            number = float(value)
            if spec.minimum is not None and number < spec.minimum:
                errors.append(f"{SchemaError.RANGE}:{spec.name}")
                continue
            if spec.maximum is not None and number > spec.maximum:
                errors.append(f"{SchemaError.RANGE}:{spec.name}")
                continue
            value = int(value) if spec.kind == "int" else number
        values[spec.name] = value
        loggable[spec.name] = "<sensitive>" if spec.sensitive else value
        if spec.is_target:
            targets.append(str(value))

    return NormalisedParameters(values=values, errors=tuple(errors),
                                loggable=loggable, target_ids=tuple(targets))


def canonical(values: dict[str, Any] | None) -> str:
    """指紋と冪等キーのための、決まった並びの文字列。

    **キー順を固定する。** dict の順序で指紋が変わると、同じ操作が
    別物になって二重実行される。
    """
    return json.dumps(dict(values or {}), sort_keys=True, ensure_ascii=False,
                      separators=(",", ":"))


# ---------------------------------------------------------------------------
# ツールと操作
# ---------------------------------------------------------------------------


class VerificationMethod(StrEnum):
    """やった結果を、何をもって「効いた」と見なすか。"""

    #: 中身のある出力が返った。読み取り専用ならこれで足りる。
    OUTPUT_PRESENT = "output_present"
    #: 対象を読み直して確かめる。
    READBACK = "readback"
    #: ファイルが存在するか。
    FILE_EXISTS = "file_exists"
    #: 送信側が成功を返したか。
    PROVIDER_ACK = "provider_ack"
    #: **確かめようがない。** `UNVERIFIED` になり、Goal は完了しない。
    NONE = "none"


@dataclass(frozen=True, slots=True)
class ToolOperation:
    """ツールが提供する操作1つ。"""

    operation_id: str
    effect_category: EffectCategory
    parameters: tuple[ParameterSpec, ...] = ()
    #: 同じ引数で2回呼んでも結果が変わらないか。
    idempotent: bool = False
    #: 実行中に止められるか。**止められないものを「止めた」と言わない。**
    cancellable: bool = True
    timeout_seconds: float = 20.0
    max_retries: int = 0
    verification: VerificationMethod = VerificationMethod.NONE
    #: 本番へ触らない試験用か。書き込みのテストはここだけで行う（第28項）。
    dry_run_only: bool = False
    #: 結果のうち、**話してよい鍵**（Phase 7B）。
    #:
    #: 許可制にしてある。禁止語の一覧にすると、ツールが新しい鍵を
    #: 返した瞬間から中身が確かめられないまま読み上げ候補になる——
    #: `next_action` のような「次にこれをやれ」がそのまま Planner へ
    #: 流れる。**宣言していない鍵は診断側**。
    user_safe_keys: tuple[str, ...] = ()
    summary: str = ""

    @property
    def action_category(self) -> ActionCategory:
        return EFFECT_TO_ACTION_CATEGORY.get(
            str(self.effect_category), ActionCategory.EXTERNAL_WRITE)

    @property
    def side_effect_free(self) -> bool:
        return self.effect_category in NO_SIDE_EFFECT

    @property
    def auto_retryable(self) -> bool:
        """**自動でやり直してよいか。**

        副作用が無く、同じ結果になると宣言されているものだけ。
        `EXTERNAL_WRITE` を「たぶん届いていないから」で送り直さない。
        """
        return (self.side_effect_free and self.idempotent
                and self.max_retries > 0)

    @property
    def needs_target(self) -> bool:
        return self.effect_category in NEEDS_TARGET

    def snapshot(self) -> dict[str, Any]:
        return {
            "operation_id": self.operation_id,
            "effect": str(self.effect_category),
            "idempotent": self.idempotent,
            "cancellable": self.cancellable,
            "timeout_s": self.timeout_seconds,
            "verification": str(self.verification),
            "dry_run_only": self.dry_run_only,
        }


@dataclass(frozen=True, slots=True)
class ToolCapability:
    """ツール1つ。**操作を持たないツールは登録できない。**"""

    tool_id: str
    operations: tuple[ToolOperation, ...]
    #: 設定でこのツールごと止められるか。既定は止まっている側。
    enabled: bool = False
    #: 有効化を読む設定キー。空なら `enabled` をそのまま使う。
    config_key: str = ""
    summary: str = ""

    def operation(self, operation_id: str) -> ToolOperation | None:
        for item in self.operations:
            if item.operation_id == str(operation_id):
                return item
        return None

    def snapshot(self) -> dict[str, Any]:
        return {
            "tool_id": self.tool_id, "enabled": self.enabled,
            "operations": [item.snapshot() for item in self.operations],
        }


class ToolRegistry:
    """登録されたツールの集まり。**ここに無いものは実行されない。**"""

    def __init__(self, capabilities: tuple[ToolCapability, ...] = ()) -> None:
        self._tools: dict[str, ToolCapability] = {}
        for item in capabilities:
            self.register(item)

    def register(self, capability: ToolCapability) -> None:
        if not capability.operations:
            raise ValueError("操作の無いツールは登録できない")
        self._tools[capability.tool_id] = capability

    def get(self, tool_id: str) -> ToolCapability | None:
        return self._tools.get(str(tool_id or ""))

    def resolve(
        self, tool_id: str, operation_id: str,
    ) -> tuple[ToolCapability | None, ToolOperation | None]:
        tool = self.get(tool_id)
        if tool is None:
            return None, None
        return tool, tool.operation(operation_id)

    def apply_config(self, config: Any) -> None:
        """設定から有効・無効を読み直す。**既定は無効側。**"""
        if config is None:
            return
        for tool_id, tool in list(self._tools.items()):
            if not tool.config_key:
                continue
            enabled = bool(config.get(tool.config_key, False))
            self._tools[tool_id] = ToolCapability(
                tool_id=tool.tool_id, operations=tool.operations,
                enabled=enabled, config_key=tool.config_key,
                summary=tool.summary)

    def set_enabled(self, tool_id: str, enabled: bool) -> None:
        """Apply a process-local session override without touching config."""
        tool = self.get(tool_id)
        if tool is None:
            return
        self._tools[tool.tool_id] = ToolCapability(
            tool_id=tool.tool_id, operations=tool.operations,
            enabled=bool(enabled), config_key=tool.config_key,
            summary=tool.summary)

    @property
    def tool_ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._tools))

    def snapshot(self) -> list[dict[str, Any]]:
        return [self._tools[key].snapshot() for key in sorted(self._tools)]


# ---------------------------------------------------------------------------
# 既定の台帳
# ---------------------------------------------------------------------------
#
# **監査（`docs/audits/2026-08-02_tool_paths.md`）で実在が確認できたものだけ。**
# 架空のツールを実経路へ載せない（第27項）。

WEB_SEARCH = ToolCapability(
    tool_id="web_search",
    config_key="tool_execution.tools.web_search_enabled",
    summary="外部の検索。読むだけ",
    operations=(
        ToolOperation(
            operation_id="search",
            effect_category=EffectCategory.READ_ONLY,
            parameters=(
                ParameterSpec("query", "str", required=True, max_length=200),
                ParameterSpec("max_results", "int", required=False,
                              minimum=1, maximum=8),
            ),
            idempotent=True, cancellable=True, timeout_seconds=12.0,
            max_retries=2, verification=VerificationMethod.OUTPUT_PRESENT,
            user_safe_keys=("text", "results", "query", "result_count"),
            summary="調べる"),
    ))

GAME_STATE = ToolCapability(
    tool_id="game_state_read_only",
    config_key="tool_execution.tools.game_state_enabled",
    summary="いまのゲーム状態を読む",
    operations=(
        ToolOperation(
            operation_id="read",
            effect_category=EffectCategory.READ_ONLY,
            parameters=(
                ParameterSpec("aspect", "str", required=False, max_length=40,
                              choices=("summary", "modules", "strikes")),
            ),
            idempotent=True, cancellable=True, timeout_seconds=3.0,
            max_retries=1, verification=VerificationMethod.OUTPUT_PRESENT,
            user_safe_keys=("summary", "modules", "strikes")),
    ))

VISION = ToolCapability(
    tool_id="vision_read_only",
    config_key="tool_execution.tools.vision_enabled",
    summary="画面を1枚読む",
    operations=(
        ToolOperation(
            operation_id="describe",
            effect_category=EffectCategory.READ_ONLY,
            parameters=(
                ParameterSpec("focus", "str", required=False, max_length=80),
            ),
            idempotent=False, cancellable=True, timeout_seconds=15.0,
            max_retries=0, verification=VerificationMethod.OUTPUT_PRESENT,
            user_safe_keys=("scene_type", "description")),
    ))

#: 書き込みの練習用。**本番のサービスへは触らない**（第28項）。
SCRATCH = ToolCapability(
    tool_id="test_scratch",
    config_key="tool_execution.tools.test_scratch_enabled",
    summary="試験用。作業フォルダ内のメモだけ",
    operations=(
        ToolOperation(
            operation_id="write_note",
            effect_category=EffectCategory.LOCAL_REVERSIBLE,
            parameters=(
                ParameterSpec("name", "str", required=True, max_length=60,
                              is_target=True),
                ParameterSpec("body", "str", required=True, max_length=400,
                              sensitive=True),
            ),
            idempotent=True, cancellable=False, timeout_seconds=5.0,
            max_retries=0, verification=VerificationMethod.FILE_EXISTS,
            dry_run_only=True, summary="メモを書く（試験用）"),
        ToolOperation(
            operation_id="notify",
            effect_category=EffectCategory.PERSON_DIRECTED,
            parameters=(
                ParameterSpec("recipient", "str", required=True, max_length=60,
                              is_target=True),
                ParameterSpec("message", "str", required=True, max_length=300,
                              sensitive=True),
            ),
            idempotent=False, cancellable=False, timeout_seconds=8.0,
            max_retries=0, verification=VerificationMethod.PROVIDER_ACK,
            dry_run_only=True, summary="送ったことにする（試験用）"),
    ))

#: 試験用の**実書き込み**（Phase 7B）。決めた root の中へテキスト1件。
#:
#: `SCRATCH` の `write_note` と分けてあるのは、あちらが `dry_run_only` の
#: 型どまりで、こちらは実際にファイルを作るため。**混ぜると、試すつもりで
#: 本当に書く／書くつもりで何も起きない、のどちらかが起きる。**
SCRATCH_CREATE = ToolCapability(
    tool_id="test_scratch_create_text",
    config_key="tools.test_scratch_enabled",
    summary="試験用。決めた root の中へテキストを1件作る",
    operations=(
        ToolOperation(
            operation_id="create",
            effect_category=EffectCategory.LOCAL_REVERSIBLE,
            parameters=(
                ParameterSpec("relative_path", "str", required=True,
                              max_length=120, is_target=True),
                ParameterSpec("content", "str", required=True, max_length=2000,
                              sensitive=True),
            ),
            # 同じ内容でもう一度呼ばれたら**失敗**する（上書きしない）ので、
            # 冪等ではない。自動 Retry もしない。
            idempotent=False, cancellable=False, timeout_seconds=5.0,
            max_retries=0, verification=VerificationMethod.FILE_EXISTS,
            user_safe_keys=("relative_path", "byte_size"),
            summary="テキストを1件作る"),
    ))

DEFAULT_CAPABILITIES: tuple[ToolCapability, ...] = (
    WEB_SEARCH, GAME_STATE, VISION, SCRATCH, SCRATCH_CREATE,
)


def default_registry(config: Any = None) -> ToolRegistry:
    registry = ToolRegistry(DEFAULT_CAPABILITIES)
    registry.apply_config(config)
    return registry


__all__ = [
    "DEFAULT_CAPABILITIES", "EFFECT_TO_ACTION_CATEGORY", "IRREVERSIBLE",
    "NEEDS_TARGET", "NO_SIDE_EFFECT", "EffectCategory", "NormalisedParameters",
    "ParameterSpec", "SchemaError", "ToolCapability", "ToolOperation",
    "ToolRegistry", "VerificationMethod", "canonical", "default_registry",
    "effect_of", "normalise_parameters",
]
