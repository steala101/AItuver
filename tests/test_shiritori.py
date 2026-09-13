from neuro_voice.dialogue.shiritori import ShiritoriCoach, first_kana, last_kana


def test_start_returns_context_not_reply():
    coach = ShiritoriCoach()

    ctx = coach.observe_user("しりとりしよう")

    assert coach.active is True
    assert ctx is not None and "しりとり開始" in ctx
    # 応答文はLLMが作る。コーチは定型の返事を持たない
    assert "私から" not in ctx


def test_normal_talk_is_ignored():
    coach = ShiritoriCoach()

    assert coach.observe_user("今日の天気いいね") is None
    assert coach.active is False


def test_tracks_chain_and_expected_kana():
    coach = ShiritoriCoach()
    coach.observe_user("しりとりしよう")

    ctx = coach.observe_user("りんご")
    assert coach.expected == "ご"
    assert "りんご" in coach.used
    assert ctx is not None and "「ご」で始まる言葉" in ctx
    assert "ごりら" in ctx  # 小型LLM向けの実例ヒント

    # AIの応答から言葉を推定して連鎖を進める
    coach.observe_assistant("じゃあ私は、ゴリラ!ごりらだよ。")
    assert coach.expected == "ら"
    assert "ごりら" in coach.used


def test_wrong_start_is_noted_for_llm():
    coach = ShiritoriCoach()
    coach.observe_user("しりとりしよう")
    coach.observe_user("りんご")

    ctx = coach.observe_user("だるま")

    assert ctx is not None and "「ご」始まりのはず" in ctx


def test_word_ending_in_n_ends_game():
    coach = ShiritoriCoach()
    coach.observe_user("しりとりしよう")

    ctx = coach.observe_user("ごはん")

    assert ctx is not None and "決着" in ctx
    assert coach.active is False


def test_stop_request_resets_state():
    coach = ShiritoriCoach()
    coach.observe_user("しりとりしよう")
    coach.observe_user("りんご")

    ctx = coach.observe_user("しりとりはもうやめよう")

    assert coach.active is False
    assert coach.used == []
    assert ctx is not None and "しりとり終了" in ctx


def test_correction_is_treated_as_meta_not_word():
    coach = ShiritoriCoach()
    coach.observe_user("しりとりしよう")
    coach.observe_user("ライス")
    assert coach.expected == "す"

    # ルール違反の指摘は言葉の提出として扱わない (連鎖を汚さない)
    ctx = coach.observe_user("最後はスだからセイタンは違うよ")

    assert coach.expected == "す"
    assert len(coach.used) == 1
    assert ctx is not None and "指摘・雑談" in ctx


def test_long_sentence_is_meta():
    coach = ShiritoriCoach()
    coach.observe_user("しりとりしよう")
    coach.observe_user("りんご")

    ctx = coach.observe_user("そういえばこの前ゲームで遊んだのを思い出したよ")

    assert coach.expected == "ご"
    assert ctx is not None and "指摘・雑談" in ctx


def test_long_vowel_uses_previous_vowel():
    assert last_kana("ゼリー") == "い"
    assert first_kana("リンゴ") == "り"
