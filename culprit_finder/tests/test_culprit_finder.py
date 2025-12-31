"""Tests for the CulpritFinder class."""

from culprit_finder import culprit_finder, github
import re
import pytest
from datetime import datetime, timezone

WORKFLOW_FILE = "test_workflow.yml"
CULPRIT_WORKFLOW = "culprit_finder.yml"
REPO = "owner/repo"


@pytest.fixture
def mock_gh_client(mocker):
  """Returns a mock GithubClient."""
  return mocker.create_autospec(github.GithubClient, instance=True)


@pytest.fixture
def finder(request, mock_gh_client):
  """Returns a CulpritFinder instance for testing."""
  has_culprit_finder_workflow = getattr(request, "param", True)
  return culprit_finder.CulpritFinder(
    repo=REPO,
    start_sha="start_sha",
    end_sha="end_sha",
    workflow_file=WORKFLOW_FILE,
    has_culprit_finder_workflow=has_culprit_finder_workflow,
    github_client=mock_gh_client,
  )


@pytest.mark.parametrize("finder", [True, False], indirect=True)
def test_wait_for_workflow_completion_success(mocker, finder, mock_gh_client):
  """
  Tests that _wait_for_workflow_completion correctly handles a successful workflow run.
  """
  mocker.patch("time.sleep", return_value=None)  # Skip sleep

  branch_name = "test-branch"
  commit_sha = "sha1"
  previous_run_id = None

  run_in_progress = {
    "headSha": commit_sha,
    "status": "in_progress",
    "createdAt": datetime.now(timezone.utc).isoformat(),
    "databaseId": 102,
  }
  run_completed = {
    "headSha": commit_sha,
    "status": "completed",
    "conclusion": "success",
    "createdAt": datetime.now(timezone.utc).isoformat(),
    "databaseId": 102,
  }

  mock_gh_client.get_latest_run.side_effect = [
    None,
    run_in_progress,
    run_completed,
  ]

  workflow = CULPRIT_WORKFLOW if finder._has_culprit_finder_workflow else WORKFLOW_FILE
  result = finder._wait_for_workflow_completion(
    workflow, branch_name, commit_sha, previous_run_id, poll_interval=0.1
  )

  assert result == run_completed
  assert mock_gh_client.get_latest_run.call_count == 3

  for call_args in mock_gh_client.get_latest_run.call_args_list:
    assert call_args[0][0] == workflow


@pytest.mark.parametrize("finder", [True, False], indirect=True)
def test_test_commit_success(mocker, finder, mock_gh_client):
  """Tests that _test_commit triggers the workflow and returns True on success."""

  branch = "test-branch"
  commit_sha = "sha1"

  mock_wait = mocker.patch.object(finder, "_wait_for_workflow_completion")
  mock_wait.return_value = {"conclusion": "success"}

  # Mock get_latest_run to return None for the "previous run" check
  mock_gh_client.get_latest_run.return_value = None

  is_good = finder._test_commit(commit_sha, branch)

  assert is_good is True

  # Determine expected arguments based on configuration
  if finder._has_culprit_finder_workflow:
    expected_workflow = CULPRIT_WORKFLOW
    expected_inputs = {"workflow-to-debug": WORKFLOW_FILE}
  else:
    expected_workflow = WORKFLOW_FILE
    expected_inputs = {}

  mock_gh_client.trigger_workflow.assert_called_once_with(
    expected_workflow,
    branch,
    expected_inputs,
  )


def test_test_commit_failure(mocker, finder, mock_gh_client):
  """Tests that _test_commit returns False if the workflow fails."""
  mock_wait = mocker.patch.object(finder, "_wait_for_workflow_completion")
  mock_wait.return_value = {"conclusion": "failure"}

  # Mock get_latest_run to return None for the "previous run" check
  mock_gh_client.get_latest_run.return_value = None

  assert finder._test_commit("sha", "branch") is False


def _create_commit(sha: str, message: str) -> github.Commit:
  return {"sha": sha, "message": message}


@pytest.mark.parametrize(
  "commits, test_results, expected_culprit_idx",
  [
    # Scenario 1: Culprit found (C1 is bad)
    # [C0 (Good), C1 (Bad), C2 (Bad)]
    # Search path: Mid=1 (C1) -> Bad. Mid=0 (C0) -> Good.
    # Result: C1 (index 1)
    (
      [
        _create_commit("c0", "m0"),
        _create_commit("c1", "m1"),
        _create_commit("c2", "m2"),
      ],
      [False, True],  # Results for checks on C1, then C0
      1,
    ),
    # Scenario 2: All commits are good
    # [C0 (Good), C1 (Good), C2 (Good)]
    # Search path: Mid=1 (C1) -> Good. Mid=2 (C2) -> Good.
    # Result: None
    (
      [
        _create_commit("c0", "m0"),
        _create_commit("c1", "m1"),
        _create_commit("c2", "m2"),
      ],
      [True, True],  # Results for checks on C1, then C2
      None,
    ),
    # Scenario 3: All commits are bad
    # [C0 (Bad), C1 (Bad), C2 (Bad)]
    # Search path: Mid=1 (C1) -> Bad. Mid=0 (C0) -> Bad.
    # Result: C0 (index 0)
    (
      [
        _create_commit("c0", "m0"),
        _create_commit("c1", "m1"),
        _create_commit("c2", "m2"),
      ],
      [False, False],  # Results for checks on C1, then C0
      0,
    ),
    # Scenario 4: No commits
    (
      [],
      [],
      None,
    ),
    # Scenario 5: Single commit is GOOD
    (
      [_create_commit("c0", "m0")],
      [True],
      None,
    ),
    # Scenario 6: Single commit is BAD
    (
      [_create_commit("c0", "m0")],
      [False],
      0,
    ),
  ],
)
def test_run_bisection(
  mocker, finder, mock_gh_client, commits, test_results, expected_culprit_idx
):
  """Tests various bisection scenarios including finding a culprit, no culprit, etc."""
  mock_gh_client.compare_commits.return_value = commits

  # Mock check_branch_exists to alternate False/True to simulate creation/deletion needs
  # We need enough values for the max possible iterations (2 * len(commits))
  mock_gh_client.check_branch_exists.side_effect = [False, True] * (len(commits) + 1)

  mock_test = mocker.patch.object(finder, "_test_commit")
  mock_test.side_effect = test_results

  culprit_commit, repo = finder.run_bisection()

  assert repo == REPO
  if expected_culprit_idx is None:
    assert culprit_commit is None
  else:
    assert culprit_commit == commits[expected_culprit_idx]

  if commits:
    # Verify compare_commits was called
    mock_gh_client.compare_commits.assert_called_once()
  else:
    # If no commits, create_branch should not be called
    mock_gh_client.create_branch.assert_not_called()


def test_run_bisection_branch_cleanup_on_failure(mocker, finder, mock_gh_client):
  """Tests that the temporary branch is deleted even if testing the commit fails."""
  commits = [{"sha": "c0", "commit": {"message": "m0"}}]
  mock_gh_client.compare_commits.return_value = commits

  # Branch doesn't exist initially (so create), but exists when cleaning up
  mock_gh_client.check_branch_exists.side_effect = [False, True]

  mock_test = mocker.patch.object(finder, "_test_commit")
  mock_test.side_effect = Exception("Something went wrong")

  with pytest.raises(Exception, match="Something went wrong"):
    finder.run_bisection()

  mock_gh_client.create_branch.assert_called_once()

  assert mock_gh_client.delete_branch.call_count == 1
  called_branch_name_delete = mock_gh_client.delete_branch.call_args[0][0]

  assert re.fullmatch(
    r"culprit-finder/test-c0_[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
    called_branch_name_delete,
  )


def test_run_bisection_branch_already_exists(mocker, finder, mock_gh_client):
  """Tests that create_branch is skipped if the branch already exists."""
  commits = [{"sha": "c0", "commit": {"message": "m0"}}]
  mock_gh_client.compare_commits.return_value = commits

  # Branch exists initially (skip create), and exists for cleanup
  mock_gh_client.check_branch_exists.return_value = True

  mocker.patch.object(finder, "_test_commit", return_value=True)

  finder.run_bisection()

  assert mock_gh_client.delete_branch.call_count == 1
  called_branch_name_delete = mock_gh_client.delete_branch.call_args[0][0]

  assert re.fullmatch(
    r"culprit-finder/test-c0_[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
    called_branch_name_delete,
  )


def test_run_bisection_cross_repo(mocker, mock_gh_client):
  """
  Tests the cross-repo bisection logic when the culprit is in a dependency.
  """
  cross_repo_dep = "owner/other-repo"
  dep_pin_file = "deps.bzl"
  start_sha = "start"
  end_sha = "end"

  finder = culprit_finder.CulpritFinder(
    repo=REPO,
    start_sha=start_sha,
    end_sha=end_sha,
    workflow_file=WORKFLOW_FILE,
    has_culprit_finder_workflow=True,
    github_client=mock_gh_client,
    cross_repo_dep=cross_repo_dep,
    dep_pin_file=dep_pin_file,
  )

  main_commits = [
    _create_commit("c0", "m0"),
    _create_commit("c1", "m1"),
  ]
  cross_commits = [
    _create_commit("cr0", "xr0"),
    _create_commit("cr1", "xr1"),
  ]

  # Setup mock responses for commit comparisons
  # First call for main repo, second call for cross repo (instantiated inside)
  mock_gh_client.compare_commits.return_value = main_commits

  # Mock reading the dependency pin file
  mock_gh_client.get_file_content.side_effect = [
    "COMMIT=cr_start",  # start_sha version
    "COMMIT=cr_end",  # end_sha version
  ]

  # Create a mock for the cross-repo GitHub client
  mock_cross_gh_client = mocker.create_autospec(github.GithubClient, instance=True)
  mock_cross_gh_client.compare_commits.return_value = cross_commits
  # Patch the GithubClient constructor to return our mock for the cross-repo dep
  mocker.patch("culprit_finder.github.GithubClient", return_value=mock_cross_gh_client)

  # Mock test results:
  # 1. Main repo bisect:
  #    - c0 (mid) -> True (Good)
  #    - c1 (mid) -> True (Good)
  #    -> Returns None (no culprit in main repo)
  # 2. Cross repo bisect (called with fixed_branch_commit=start_sha)
  #    - cr0 (mid) -> True (Good)
  #    - cr1 (mid) -> False (Bad)
  #    -> Returns cr1
  mock_test = mocker.patch.object(finder, "_test_commit")
  mock_test.side_effect = [True, True, True, False]

  # Mock branch existence checks for both bisect runs
  mock_gh_client.check_branch_exists.return_value = False

  culprit_commit, repo = finder.run_bisection()

  assert repo == cross_repo_dep
  assert culprit_commit == cross_commits[1]  # cr1 is the culprit

  assert mock_gh_client.compare_commits.call_count == 1
  mock_cross_gh_client.compare_commits.assert_called_once_with("cr_start", "cr_end")

  assert mock_test.call_count == 4

  # Check specific call arguments for cross-repo tests
  # The 3rd and 4th calls are for cross-repo commits cr0 and cr1
  # They should be tested on a branch created from 'start_sha' (fixed_branch_commit)
  # and have the dependency pinned to the cross-repo commit
  args_cr0 = mock_test.call_args_list[2]
  assert args_cr0[0][0] == start_sha  # test_commit is start_sha
  assert args_cr0[1]["dep_pin_commit"] == "cr0"

  args_cr1 = mock_test.call_args_list[3]
  assert args_cr1[0][0] == start_sha  # test_commit is start_sha
  assert args_cr1[1]["dep_pin_commit"] == "cr1"


def test_get_cross_repo_commits_success(mock_gh_client):
  """Tests extracting cross-repo commit SHAs."""
  dep_pin_file = "deps.bzl"
  start_sha = "start_sha"
  end_sha = "end_sha"

  finder = culprit_finder.CulpritFinder(
    repo=REPO,
    start_sha=start_sha,
    end_sha=end_sha,
    workflow_file=WORKFLOW_FILE,
    has_culprit_finder_workflow=True,
    github_client=mock_gh_client,
    dep_pin_file=dep_pin_file,
  )

  mock_gh_client.get_file_content.side_effect = [
    "COMMIT=sha123",
    "COMMIT=sha456",
  ]

  start, end = finder._get_cross_repo_commits()

  assert start == "sha123"
  assert end == "sha456"

  assert mock_gh_client.get_file_content.call_count == 2
  mock_gh_client.get_file_content.assert_any_call(dep_pin_file, start_sha)
  mock_gh_client.get_file_content.assert_any_call(dep_pin_file, end_sha)


def test_get_cross_repo_commits_no_file(mock_gh_client):
  """Tests that ValueError is raised when dep_pin_file is missing."""
  finder = culprit_finder.CulpritFinder(
    repo=REPO,
    start_sha="start_sha",
    end_sha="end_sha",
    workflow_file=WORKFLOW_FILE,
    has_culprit_finder_workflow=True,
    github_client=mock_gh_client,
    dep_pin_file=None,
  )

  with pytest.raises(ValueError, match="Dependency pin file is not specified"):
    finder._get_cross_repo_commits()
