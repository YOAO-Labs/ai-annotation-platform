from pydantic import ValidationError

from app.schemas.user import UserPreferences


def test_workbench_layout_preferences_accept_camelcase_and_dump_aliases():
    prefs = UserPreferences.model_validate(
        {
            "workbench": {
                "image": {"smoothImage": False},
                "layout": {
                    "leftOpen": False,
                    "rightOpen": True,
                    "floatingTaskQueue": {
                        "detached": True,
                        "x": 24,
                        "y": 72,
                        "w": 320,
                        "h": 620,
                    },
                    "floatingClassPalette": {
                        "detached": True,
                        "x": 24,
                        "y": 420,
                        "w": 300,
                        "h": 420,
                    },
                    "floatingInspector": {
                        "detached": True,
                        "x": 640,
                        "y": 80,
                        "w": 360,
                        "h": 600,
                    },
                    "floatingDiscussion": {
                        "detached": True,
                        "x": 760,
                        "y": 180,
                        "w": 420,
                        "h": 560,
                    },
                    "floatingSelection": {
                        "collapsed": True,
                        "x": 900,
                        "y": 120,
                        "w": 340,
                        "h": 440,
                    },
                    "triViewFloat": {
                        "collapsed": True,
                        "x": 720,
                        "y": 120,
                        "w": 300,
                        "h": 500,
                    },
                },
            }
        }
    )

    layout = prefs.workbench.layout
    assert layout.left_open is False
    assert layout.right_open is True
    assert layout.floating_task_queue is not None
    assert layout.floating_task_queue.detached is True
    assert layout.floating_class_palette is not None
    assert layout.floating_class_palette.w == 300
    assert layout.floating_inspector is not None
    assert layout.floating_inspector.detached is True
    assert layout.floating_discussion is not None
    assert layout.floating_discussion.h == 560
    assert layout.floating_selection is not None
    assert layout.floating_selection.collapsed is True
    assert layout.floating_selection.x == 900
    assert layout.tri_view_float is not None
    assert layout.tri_view_float.collapsed is True

    dumped = prefs.model_dump(mode="json", exclude_unset=True, by_alias=True)
    assert dumped["workbench"]["image"]["smoothImage"] is False
    assert dumped["workbench"]["layout"]["leftOpen"] is False
    assert dumped["workbench"]["layout"]["floatingTaskQueue"]["detached"] is True
    assert dumped["workbench"]["layout"]["floatingClassPalette"]["w"] == 300
    assert dumped["workbench"]["layout"]["floatingInspector"]["w"] == 360
    assert dumped["workbench"]["layout"]["floatingDiscussion"]["h"] == 560
    assert dumped["workbench"]["layout"]["floatingSelection"]["collapsed"] is True
    assert dumped["workbench"]["layout"]["floatingSelection"]["w"] == 340
    assert dumped["workbench"]["layout"]["triViewFloat"]["h"] == 500


def test_workbench_layout_preferences_keep_default_subtree():
    prefs = UserPreferences.model_validate({})

    assert prefs.named_presets_revision == "0"
    assert prefs.workbench.image.smoothImage is True
    assert prefs.workbench.layout.left_open is None
    assert prefs.workbench.layout.floating_task_queue is None
    assert prefs.workbench.layout.floating_class_palette is None
    assert prefs.workbench.layout.floating_inspector is None
    assert prefs.workbench.layout.floating_discussion is None


def test_preferences_top_level_merge_contract_keeps_other_subtrees():
    existing = {
        "workbench": {"image": {"smoothImage": True}},
        "ai": {"params_by_backend": {"sam": {"score_threshold": 0.7}}},
    }
    incoming = UserPreferences.model_validate(
        {
            "workbench": {
                "image": {"smoothImage": False},
                "common": {"rightWidthPct": 30},
            }
        }
    ).model_dump(mode="json", exclude_unset=True, by_alias=True)

    merged = {**existing, **incoming}

    assert merged["ai"] == existing["ai"]
    assert merged["workbench"]["image"]["smoothImage"] is False
    assert merged["workbench"]["common"]["rightWidthPct"] == 30


def test_workbench_layout_preferences_reject_out_of_range_sizes():
    try:
        UserPreferences.model_validate(
            {
                "workbench": {
                    "common": {"leftWidthPct": 5, "rightWidthPct": 40},
                    "layout": {"triViewFloat": {"w": 800}},
                }
            }
        )
    except ValidationError as exc:
        errors = {(tuple(err["loc"]), err["type"]) for err in exc.errors()}
    else:  # pragma: no cover
        raise AssertionError("expected validation error")

    assert (("workbench", "common", "leftWidthPct"), "greater_than_equal") in errors
    assert (("workbench", "common", "rightWidthPct"), "less_than_equal") in errors
    assert (
        ("workbench", "layout", "triViewFloat", "w"),
        "less_than_equal",
    ) in errors


def test_workbench_sidebar_width_pct_in_range_and_default():
    prefs = UserPreferences.model_validate(
        {"workbench": {"common": {"leftWidthPct": 12, "rightWidthPct": 30}}}
    )
    assert prefs.workbench.common.leftWidthPct == 12
    assert prefs.workbench.common.rightWidthPct == 30

    default = UserPreferences.model_validate({})
    assert default.workbench.common.leftWidthPct == 15
    assert default.workbench.common.rightWidthPct == 15


def test_onboarding_preferences_are_versioned_per_project():
    prefs = UserPreferences.model_validate(
        {
            "onboarding": {
                "projects": {
                    "project-a": {
                        "guide_version": "guide-v1-abcd",
                        "dismissed": True,
                        "guide_read": False,
                    }
                }
            }
        }
    )

    state = prefs.onboarding.projects["project-a"]
    assert state.guide_version == "guide-v1-abcd"
    assert state.dismissed is True
    assert state.guide_read is False

    dumped = prefs.model_dump(mode="json", exclude_unset=True, by_alias=True)
    assert dumped["onboarding"]["projects"]["project-a"] == {
        "guide_version": "guide-v1-abcd",
        "dismissed": True,
        "guide_read": False,
    }


def test_onboarding_preferences_reject_unknown_project_state_fields():
    try:
        UserPreferences.model_validate(
            {
                "onboarding": {
                    "projects": {
                        "project-a": {
                            "guide_version": "guide-v1-abcd",
                            "completed": True,
                        }
                    }
                }
            }
        )
    except ValidationError as exc:
        errors = {(tuple(err["loc"]), err["type"]) for err in exc.errors()}
    else:  # pragma: no cover
        raise AssertionError("expected validation error")

    assert (
        ("onboarding", "projects", "project-a", "completed"),
        "extra_forbidden",
    ) in errors


# ── v0.24 · workbench.shortcuts（账号级快捷键覆盖）────────────────────────


def test_shortcuts_accept_valid_binding_entry_and_dump_by_alias():
    prefs = UserPreferences.model_validate(
        {
            "workbench": {
                "shortcuts": {
                    "schemaVersion": 1,
                    "image": {
                        "image.tool.box": [
                            {"key": "k", "modifiers": []},
                            {"key": "1", "modifiers": ["alt"]},
                        ]
                    },
                    "video": {"video.tool.mask": None},
                    "common": {
                        "common.task.next": [
                            {"key": "arrowright", "modifiers": ["mod"]}
                        ]
                    },
                }
            }
        }
    )

    shortcuts = prefs.workbench.shortcuts
    assert shortcuts is not None
    assert shortcuts.schemaVersion == 1
    assert shortcuts.image["image.tool.box"][0].key == "k"
    assert shortcuts.image["image.tool.box"][1].modifiers == ["alt"]
    assert shortcuts.video["video.tool.mask"] is None

    dumped = prefs.model_dump(mode="json", exclude_unset=True, by_alias=True)
    shortcuts_dumped = dumped["workbench"]["shortcuts"]
    assert shortcuts_dumped["schemaVersion"] == 1
    assert shortcuts_dumped["image"]["image.tool.box"] == [
        {"key": "k", "modifiers": []},
        {"key": "1", "modifiers": ["alt"]},
    ]
    assert shortcuts_dumped["video"]["video.tool.mask"] is None


def test_shortcuts_reject_unknown_command_id():
    try:
        UserPreferences.model_validate(
            {
                "workbench": {
                    "shortcuts": {
                        "image": {"legacy.command": [{"key": "k", "modifiers": []}]}
                    }
                }
            }
        )
    except ValidationError:
        pass
    else:  # pragma: no cover
        raise AssertionError("expected validation error for unknown command id")


def test_shortcuts_reject_modifier_only_key_and_over_limit_bindings():
    # 仅修饰键不是合法 key
    try:
        UserPreferences.model_validate(
            {
                "workbench": {
                    "shortcuts": {
                        "image": {"image.tool.box": [{"key": "shift", "modifiers": []}]}
                    }
                }
            }
        )
    except ValidationError:
        pass
    else:  # pragma: no cover
        raise AssertionError("expected validation error for modifier-only key")

    # 每条命令最多一主一备两条组合
    try:
        UserPreferences.model_validate(
            {
                "workbench": {
                    "shortcuts": {
                        "image": {
                            "image.tool.box": [
                                {"key": "b", "modifiers": []},
                                {"key": "1", "modifiers": ["alt"]},
                                {"key": "x", "modifiers": []},
                            ]
                        }
                    }
                }
            }
        )
    except ValidationError:
        pass
    else:  # pragma: no cover
        raise AssertionError("expected validation error for over-limit binding list")


def test_shortcuts_reject_unknown_modifier_and_extra_fields():
    try:
        UserPreferences.model_validate(
            {
                "workbench": {
                    "shortcuts": {
                        "image": {
                            "image.tool.box": [{"key": "b", "modifiers": ["hyper"]}]
                        }
                    }
                }
            }
        )
    except ValidationError:
        pass
    else:  # pragma: no cover
        raise AssertionError("expected validation error for unknown modifier")

    try:
        UserPreferences.model_validate(
            {
                "workbench": {
                    "shortcuts": {
                        "image": {
                            "image.tool.box": [
                                {"key": "b", "modifiers": [], "label": "矩形"}
                            ]
                        }
                    }
                }
            }
        )
    except ValidationError:
        pass
    else:  # pragma: no cover
        raise AssertionError("expected validation error for extra binding field")


def test_shortcuts_empty_object_uses_defaults_without_migration():
    prefs = UserPreferences.model_validate({})
    shortcuts = prefs.workbench.shortcuts
    assert shortcuts is None or shortcuts == shortcuts.model_validate({})
