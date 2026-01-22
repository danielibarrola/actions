import yaml

from culprit_finder import github_client


class CulpritWorkflowValidator:
  """Validates the culprit finder workflow file."""

  def __init__(
    self,
    start_sha: str,
    end_sha: str,
    workflow_to_debug: str,
    gh_client: github_client.GithubClient,
  ):
    self._start_sha = start_sha
    self._end_sha = end_sha
    self._workflow_to_debug = workflow_to_debug
    self._gh_client = gh_client

  def _validate_culprit_finder_workflow(self, culprit_workflow_yml: str):
    """Raises an error if the workflow to debug is not called from the culprit finder workflow.

    Args:
        culprit_workflow_yml (str): yml contents of the culprit finder workflow.
        workflow_to_debug (str): Name of the workflow to debug.

    Raises:
        ValueError: If the workflow to debug is not called from the culprit finder workflow.

    """
    yml_content = yaml.safe_load(culprit_workflow_yml)
    jobs: dict[str, dict] = yml_content.get("jobs", {})

    for job_config in jobs.values():
      called_workflow_path = job_config.get("uses", "")
      called_workflow_name = called_workflow_path.split("/")[-1]
      if called_workflow_name == self._workflow_to_debug:
        return

    raise ValueError(
      f"Workflow {self._workflow_to_debug} is not called from culprit finder workflow."
    )

  def validate(self):
    for commit_sha in [self._start_sha, self._end_sha]:
      commit = self._gh_client.get_commit(commit_sha)
      culprit_workflow_yml = self._gh_client.get_file_content(
        ".github/workflows/culprit_finder.yml", commit.sha
      )
      self._validate_culprit_finder_workflow(culprit_workflow_yml)
