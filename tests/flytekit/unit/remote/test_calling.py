import typing
from collections import OrderedDict

import pytest

from flytekit.core import context_manager
from flytekit.core.context_manager import Image, ImageConfig
from flytekit.core.launch_plan import LaunchPlan
from flytekit.models.task import TaskTemplate
from flytekit.models.core.workflow import WorkflowTemplate
from flytekit.core.task import task
from flytekit.core.workflow import workflow
from flytekit.remote import FlyteLaunchPlan, FlyteTask
from flytekit.remote.workflow import FlyteWorkflow
from flytekit.remote.interface import TypedInterface
from flytekit.tools.translator import gather_dependent_entities, get_serializable

default_img = Image(name="default", fqn="test", tag="tag")
serialization_settings = context_manager.SerializationSettings(
    project="project",
    domain="domain",
    version="version",
    env=None,
    image_config=ImageConfig(default_image=default_img, images=[default_img]),
)


@task
def t1(a: int) -> int:
    return a + 2


@task
def t2(a: int, b: str) -> str:
    return b + str(a)


@workflow
def sub_wf(a: int, b: str) -> (int, str):
    x = t1(a=a)
    d = t2(a=x, b=b)
    return x, d


serialized = OrderedDict()
t1_spec = get_serializable(serialized, serialization_settings, t1)
ft = FlyteTask.promote_from_model(t1_spec.template)


def test_fetched_task():
    @workflow
    def wf(a: int) -> int:
        return ft(a=a).with_overrides(node_name="foobar")

    # Should not work unless mocked out.
    with pytest.raises(Exception, match="cannot be run locally"):
        wf(a=3)

    # Should have one task template
    serialized = OrderedDict()
    wf_spec = get_serializable(serialized, serialization_settings, wf)
    vals = [v for v in serialized.values()]
    tts = [f for f in filter(lambda x: isinstance(x, TaskTemplate), vals)]
    assert len(tts) == 1
    assert wf_spec.template.nodes[0].id == "foobar"
    assert wf_spec.template.outputs[0].binding.promise.node_id == "foobar"


def test_calling_lp():
    sub_wf_lp = LaunchPlan.get_or_create(sub_wf)
    serialized = OrderedDict()
    lp_model = get_serializable(serialized, serialization_settings, sub_wf_lp)
    task_templates, wf_specs, lp_specs = gather_dependent_entities(serialized)
    for wf_id, spec in wf_specs.items():
        break

    remote_lp = FlyteLaunchPlan.promote_from_model(lp_model.id, lp_model.spec)
    # To pretend that we've fetched this launch plan from Admin, also fill in the Flyte interface, which isn't
    # part of the IDL object but is something FlyteRemote does
    remote_lp._interface = TypedInterface.promote_from_model(spec.template.interface)
    serialized = OrderedDict()

    @workflow
    def wf2(a: int) -> typing.Tuple[int, str]:
        return remote_lp(a=a, b="hello")

    wf_spec = get_serializable(serialized, serialization_settings, wf2)
    print(wf_spec.template.nodes[0].workflow_node.launchplan_ref)
    assert wf_spec.template.nodes[0].workflow_node.launchplan_ref == lp_model.id


def test_dynamic():
    ...


def test_calling_wf():
    serialized = OrderedDict()
    wf_spec = get_serializable(serialized, serialization_settings, sub_wf)
    task_templates, wf_specs, lp_specs = gather_dependent_entities(serialized)
    fwf = FlyteWorkflow.promote_from_model(wf_spec.template, tasks=task_templates)

    @workflow
    def parent_1(a: int, b: str) -> typing.Tuple[int, str]:
        y = t1(a=a)
        return fwf(a=y, b=b)

    serialized = OrderedDict()
    wf_spec = get_serializable(serialized, serialization_settings, parent_1)
    # Get task_specs from the second one, merge with the first one. Admin normally would be the one to do this.
    task_templates_p1, wf_specs, lp_specs = gather_dependent_entities(serialized)
    for k, v in task_templates.items():
        task_templates_p1[k] = v

    # Pick out the subworkflow templates from the ordereddict. We can't use the output of the gather_dependent_entities
    # function because that only looks for WorkflowSpecs
    subwf_templates = {x.id: x for x in list(filter(lambda x: isinstance(x, WorkflowTemplate), serialized.values()))}
    fwf_p1 = FlyteWorkflow.promote_from_model(wf_spec.template, sub_workflows=subwf_templates, tasks=task_templates_p1)

    @workflow
    def parent_2(a: int, b: str) -> typing.Tuple[int, str]:
        x, y = fwf_p1(a=a, b=b)
        z = t1(a=x)
        return z, y

    serialized = OrderedDict()
    wf_spec = get_serializable(serialized, serialization_settings, parent_2)
    # Make sure both were picked up.
    assert len(wf_spec.sub_workflows) == 2
    print(wf_spec)
