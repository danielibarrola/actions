import textwrap

import pytest

from culprit_finder import workflow_validation

CULPRIT_WORKFLOW = textwrap.dedent(
  """
    name: Culprit Finder Workflow

    on:
      workflow_dispatch:
        inputs:
          workflow-to-debug:
            description: "Which workflow should be run?"
            type: string
            default: ""
            required: true

    jobs:
      bisect-test-workflow:
        if: ${{ inputs.workflow-to-debug == 'run_tests.yml' }}
        uses: ./.github/workflows/run_tests.yml
        name: Bisect Test Workflow

      bisect-build-workflow:
        if: ${{ inputs.workflow-to-debug == 'build.yml' }}
        uses: ./.github/workflows/build.yml
        name: Bisect Build Workflow
    """
)


@pytest.mark.parametrize("workflow_to_debug", ["run_tests.yml", "build.yml"])
def test_workflow_validation_success(workflow_to_debug: str):
  workflow_validation.validate_culprit_finder_workflow(
    CULPRIT_WORKFLOW, workflow_to_debug
  )


def test_raises_error_on_invalid_workflow():
  with pytest.raises(ValueError):
    workflow_validation.validate_culprit_finder_workflow(
      CULPRIT_WORKFLOW, "invalid_workflow.yml"
    )
