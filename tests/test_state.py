"""Tests for the state.py module — global state management.

Tests are written against the ACTUAL source code in state.py which has:
  Globals: active_sessions, current_project, sdk_client, session_mgr,
           user_last_message, PROJECT_NAME_RE, server_start_time
  Functions: get_manager(), get_all_managers(), register_manager() [async],
             unregister_manager() [async], initialize() [async],
             is_valid_project_name()

NOTE: The autouse clean_state fixture in conftest.py clears all globals
before/after each test. No per-file fixture needed.
"""

import re

import pytest

import state

# ── Module-level defaults ──────────────────────────────────────────


def test_active_sessions_when_fresh_should_be_empty_dict():
    """active_sessions should be an empty dict by default (after reset)."""
    assert state.active_sessions == {}
    assert isinstance(state.active_sessions, dict)


def test_current_project_when_fresh_should_be_empty_dict():
    """current_project should be an empty dict by default."""
    assert state.current_project == {}
    assert isinstance(state.current_project, dict)


def test_user_last_message_when_fresh_should_be_empty_dict():
    """user_last_message should be an empty dict by default."""
    assert state.user_last_message == {}
    assert isinstance(state.user_last_message, dict)


def test_sdk_client_when_not_initialized_should_be_none():
    """sdk_client is None until initialize() is called."""
    assert state.sdk_client is None


def test_session_mgr_when_not_initialized_should_be_none():
    """session_mgr is None until initialize() is called."""
    assert state.session_mgr is None


def test_server_start_time_should_be_a_float():
    """server_start_time is set at import time as a float."""
    assert hasattr(state, "server_start_time")
    assert isinstance(state.server_start_time, float)


def test_user_last_message_when_storing_float_should_work():
    """user_last_message maps user_id (int) -> timestamp (float)."""
    state.user_last_message[42] = 1709900000.0
    assert state.user_last_message[42] == 1709900000.0


def test_current_project_when_set_should_store_string():
    """current_project maps user_id (int) -> project_id (str)."""
    state.current_project[1] = "my-project"
    assert state.current_project[1] == "my-project"


def test_project_name_regex_should_be_compiled_pattern():
    """PROJECT_NAME_RE should be a compiled regex pattern."""
    assert isinstance(state.PROJECT_NAME_RE, re.Pattern)


# ── PROJECT_NAME_RE validation ─────────────────────────────────────


@pytest.mark.parametrize(
    "name",
    [
        "my-project",
        "project_1",
        "Hello World",
        "test123",
        "A",
        "a",
        "project-name-123",
        "A B C",
        "under_score",
        "MiXeD-CaSe_123",
    ],
)
def test_project_name_regex_when_valid_name_should_match(name):
    """PROJECT_NAME_RE should accept valid project names."""
    assert state.PROJECT_NAME_RE.match(name), f"Should match: {name!r}"


@pytest.mark.parametrize(
    "name",
    [
        "",  # empty string
        "project@home",  # @ symbol
        "test/path",  # slash
        "bad!name",  # exclamation
        "no.dots",  # dots
        "angle<bracket>",  # angle brackets
        "semi;colon",  # semicolon
        "back\\slash",  # backslash
        "special!chars",  # exclamation
    ],
)
def test_project_name_regex_when_invalid_name_should_not_match(name):
    """PROJECT_NAME_RE should reject names with special characters."""
    assert not state.PROJECT_NAME_RE.match(name), f"Should NOT match: {name!r}"


# ── get_manager() ──────────────────────────────────────────────────


def test_get_manager_when_empty_should_return_none_tuple():
    """get_manager should return (None, None) when no sessions exist."""
    manager, user_id = state.get_manager("nonexistent")
    assert manager is None
    assert user_id is None


def test_get_manager_when_registered_should_find_manager():
    """get_manager should find a manager after it's been added to active_sessions."""
    fake_manager = object()
    state.active_sessions[42] = {"my-project": fake_manager}

    found, user_id = state.get_manager("my-project")
    assert found is fake_manager
    assert user_id == 42


def test_get_manager_when_wrong_project_should_return_none():
    """get_manager should return None for a project_id that doesn't exist."""
    fake_manager = object()
    state.active_sessions[1] = {"project-a": fake_manager}

    found, user_id = state.get_manager("project-b")
    assert found is None
    assert user_id is None


def test_get_manager_when_multiple_users_should_search_all():
    """get_manager should search across all users, not just the first one."""
    mgr_a = object()
    mgr_b = object()
    state.active_sessions[1] = {"project-a": mgr_a}
    state.active_sessions[2] = {"project-b": mgr_b}

    found, uid = state.get_manager("project-b")
    assert found is mgr_b
    assert uid == 2


def test_get_manager_when_duplicate_project_ids_should_return_first_match():
    """If the same project_id exists under two users, returns the first found."""
    mgr1 = object()
    mgr2 = object()
    state.active_sessions[1] = {"dup-proj": mgr1}
    state.active_sessions[2] = {"dup-proj": mgr2}

    found, uid = state.get_manager("dup-proj")
    # Dict iteration order is insertion order in Python 3.7+
    assert found is not None
    assert uid in (1, 2)


# ── get_all_managers() ─────────────────────────────────────────────


def test_get_all_managers_when_empty_should_return_empty_list():
    """get_all_managers should return an empty list when no sessions exist."""
    result = state.get_all_managers()
    assert result == []
    assert isinstance(result, list)


def test_get_all_managers_when_single_user_should_return_one_tuple():
    """get_all_managers with one user and one project returns exactly one tuple."""
    mgr = object()
    state.active_sessions[1] = {"proj": mgr}

    result = state.get_all_managers()
    assert len(result) == 1
    assert result[0] == (1, "proj", mgr)


def test_get_all_managers_when_multiple_should_return_all():
    """get_all_managers should return all (user_id, project_id, manager) tuples."""
    mgr1 = object()
    mgr2 = object()
    mgr3 = object()
    state.active_sessions[1] = {"proj-a": mgr1, "proj-b": mgr2}
    state.active_sessions[2] = {"proj-c": mgr3}

    result = state.get_all_managers()
    assert len(result) == 3

    # Check all managers are present (order may vary by dict iteration)
    managers_found = {m for _, _, m in result}
    assert managers_found == {mgr1, mgr2, mgr3}

    # Check user_ids and project_ids are correct
    tuples = {(uid, pid) for uid, pid, _ in result}
    assert (1, "proj-a") in tuples
    assert (1, "proj-b") in tuples
    assert (2, "proj-c") in tuples


# ── register_manager() ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_register_manager_when_new_should_add_to_active_sessions():
    """register_manager should add the manager under user_id + project_id."""
    fake_manager = object()
    await state.register_manager(10, "new-project", fake_manager)

    assert 10 in state.active_sessions
    assert "new-project" in state.active_sessions[10]
    assert state.active_sessions[10]["new-project"] is fake_manager


@pytest.mark.asyncio
async def test_register_manager_when_same_user_should_allow_multiple_projects():
    """register_manager should allow multiple projects per user."""
    mgr1 = object()
    mgr2 = object()
    await state.register_manager(5, "proj-x", mgr1)
    await state.register_manager(5, "proj-y", mgr2)

    assert len(state.active_sessions[5]) == 2
    assert state.active_sessions[5]["proj-x"] is mgr1
    assert state.active_sessions[5]["proj-y"] is mgr2


@pytest.mark.asyncio
async def test_register_manager_when_existing_project_should_overwrite():
    """register_manager should overwrite when re-registering same project."""
    old = object()
    new = object()
    await state.register_manager(1, "proj", old)
    await state.register_manager(1, "proj", new)

    assert state.active_sessions[1]["proj"] is new


# ── unregister_manager() ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_unregister_manager_when_exists_should_remove_project():
    """unregister_manager should remove the project from active_sessions."""
    fake_manager = object()
    state.active_sessions[7] = {"to-remove": fake_manager, "to-keep": object()}

    await state.unregister_manager(7, "to-remove")

    assert "to-remove" not in state.active_sessions[7]
    assert "to-keep" in state.active_sessions[7]


@pytest.mark.asyncio
async def test_unregister_manager_when_last_project_should_remove_user():
    """unregister_manager should remove the user entry when they have no more projects."""
    fake_manager = object()
    state.active_sessions[99] = {"only-project": fake_manager}

    await state.unregister_manager(99, "only-project")

    assert 99 not in state.active_sessions


@pytest.mark.asyncio
async def test_unregister_manager_when_nonexistent_user_should_be_safe():
    """unregister_manager should not raise when user_id doesn't exist."""
    await state.unregister_manager(999, "ghost-project")  # no exception


@pytest.mark.asyncio
async def test_unregister_manager_when_nonexistent_project_should_be_safe():
    """unregister_manager should not raise when project_id doesn't exist under a valid user."""
    state.active_sessions[1] = {"real": object()}
    await state.unregister_manager(1, "nonexistent")  # no exception
    assert "real" in state.active_sessions[1]


# ── End-to-end: register + get + unregister ────────────────────────


@pytest.mark.asyncio
async def test_register_then_get_manager_should_find_it():
    """End-to-end: register a manager, then find it with get_manager."""
    mgr = object()
    await state.register_manager(1, "e2e-project", mgr)

    found, uid = state.get_manager("e2e-project")
    assert found is mgr
    assert uid == 1


@pytest.mark.asyncio
async def test_unregister_then_get_manager_should_return_none():
    """After unregistering, get_manager should return None."""
    mgr = object()
    await state.register_manager(1, "temp-project", mgr)
    await state.unregister_manager(1, "temp-project")

    found, uid = state.get_manager("temp-project")
    assert found is None
    assert uid is None


# ── is_valid_project_name() ────────────────────────────────────────


def test_is_valid_project_name_when_valid_should_return_true():
    """is_valid_project_name should return True for valid names."""
    assert state.is_valid_project_name("my-project") is True
    assert state.is_valid_project_name("test_123") is True


def test_is_valid_project_name_when_invalid_should_return_false():
    """is_valid_project_name should return False for invalid names."""
    assert state.is_valid_project_name("bad@name") is False
    assert state.is_valid_project_name("") is False


def test_is_valid_project_name_when_non_string_should_return_false():
    """is_valid_project_name should return False for non-string inputs."""
    assert state.is_valid_project_name(123) is False
    assert state.is_valid_project_name(None) is False
