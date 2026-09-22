"""What a harness is handed: the tools as JSON Schema, and one call dispatched against them.

The registry these two modules publish (`beamer2slides.agent.tools`) is not what is tested here:
a schema test that walks the real tools would pass on the day the real tools are all wrong. What
is tested is the *rule* - a parameter's type, default and description come from the function's
own signature, a description that is missing is loud rather than quiet, and an argument dict a
model got wrong comes back as a `bad_request` Result rather than as a traceback - against stub
tools written in this file, which is also the only way to test the refusal paths deliberately.

Offline, no Google, no files touched.
"""

import asyncio
import sys
from types import ModuleType
from typing import Annotated

import pytest

from beamer2slides.agent import mcp, schema
from beamer2slides.agent.context import (LOCAL_ONLY, READ_ONLY, AgentContext, Job, tool)
from beamer2slides.agent.types import READS, READS_GOOGLE, WRITES, WRITES_GOOGLE, Refused
from beamer2slides.agent.workspace import LocalWorkspace


class _Available:
    """A credential source that has an account, so the gate answers about permission."""

    def credentials(self):
        return object()

    def describe(self):
        return {"available": True, "source": "test"}


# -- the stubs ---------------------------------------------------------------------------------

@tool("stub_inspect", needs=(READS,))
def stub_inspect(j: Job,
                 pdf: Annotated[str, "The PDF to look at, as a workspace-relative path."],
                 pages: Annotated[int, "How many pages to read."] = 3,
                 deep: Annotated[bool, "Classify as well as extract."] = False,
                 out: Annotated[str | None, "Where to write the report; omitted means nowhere."] = None,
                 only: Annotated[list[str], "Element kinds to keep."] = (),
                 ) -> None:
    """Look at a PDF and say what is in it.

    A second paragraph, so the test can see the whole docstring travels.
    """
    j.summary = f"{pdf}: {pages} pages"
    j.data = {"pdf": pdf, "pages": pages, "deep": deep, "out": out, "only": list(only)}


@tool("stub_sync", needs=(READS, WRITES, READS_GOOGLE, WRITES_GOOGLE))
def stub_sync(j: Job,
              pdf: Annotated[str, "The recompiled PDF."],
              deck: Annotated[str, "A Slides URL, an id, or an out folder."],
              dry_run: Annotated[bool, "Plan the merge without touching the deck."] = False,
              ) -> None:
    """Merge a recompiled source into a deck someone has edited."""
    j.summary = f"{pdf} -> {deck}"


@tool("stub_refuses", needs=(READS,))
def stub_refuses(j: Job,
                 deck: Annotated[str, "The deck that will be refused."],
                 ) -> None:
    """Always refuse, the way the library refuses a rebuild over someone's edits."""
    raise Refused("deck_edited", f"{deck} was edited in Slides.", slides=[4])


@tool("stub_local_write", needs=(READS, WRITES))
def stub_local_write(j: Job) -> None:
    """Write a file and nothing else; takes no parameters at all."""
    j.summary = "wrote nothing, being a stub"


STUBS = {"stub_inspect": stub_inspect, "stub_sync": stub_sync,
         "stub_refuses": stub_refuses, "stub_local_write": stub_local_write}


@pytest.fixture
def ctx(tmp_path) -> AgentContext:
    return AgentContext.offline(tmp_path)


# -- the schema --------------------------------------------------------------------------------

def test_a_schema_says_what_the_signature_says():
    s = schema.tool_schema(stub_inspect)
    assert s["name"] == "stub_inspect"
    assert s["description"].startswith("Look at a PDF")
    assert "second paragraph" in s["description"]

    body = s["input_schema"]
    assert body["type"] == "object"
    assert body["additionalProperties"] is False
    assert body["required"] == ["pdf"]                      # the only one with no default
    assert list(body["properties"]) == ["pdf", "pages", "deep", "out", "only"]


def test_every_supported_type_maps_to_its_json_type():
    props = schema.tool_schema(stub_inspect)["input_schema"]["properties"]
    assert props["pdf"]["type"] == "string"
    assert props["pages"]["type"] == "integer"
    assert props["deep"]["type"] == "boolean"
    assert props["only"] == {"type": "array", "items": {"type": "string"},
                             "description": "Element kinds to keep.", "default": []}


def test_an_optional_string_is_published_as_string_or_null():
    prop = schema.tool_schema(stub_inspect)["input_schema"]["properties"]["out"]
    assert prop["type"] == ["string", "null"]
    assert prop["default"] is None


def test_defaults_and_descriptions_travel_with_the_property():
    props = schema.tool_schema(stub_inspect)["input_schema"]["properties"]
    assert props["pages"]["default"] == 3
    assert props["deep"]["default"] is False
    assert props["pdf"]["description"] == "The PDF to look at, as a workspace-relative path."
    assert "default" not in props["pdf"]                    # required parameters have none


def test_a_tool_with_no_parameters_publishes_an_empty_object():
    body = schema.tool_schema(stub_local_write)["input_schema"]
    assert body["properties"] == {} and body["required"] == []


def test_a_parameter_with_no_description_is_refused_by_name():
    @tool("stub_bare", needs=(READS,))
    def stub_bare(j: Job, pdf: str) -> None:
        """A tool whose author forgot the description."""

    with pytest.raises(schema.SchemaError) as caught:
        schema.tool_schema(stub_bare)
    said = str(caught.value)
    assert "stub_bare" in said and "pdf" in said


def test_an_empty_description_is_refused_too():
    @tool("stub_empty", needs=(READS,))
    def stub_empty(j: Job, pdf: Annotated[str, "   "]) -> None:
        """A tool whose description is whitespace."""

    with pytest.raises(schema.SchemaError) as caught:
        schema.tool_schema(stub_empty)
    assert "stub_empty" in str(caught.value) and "pdf" in str(caught.value)


def test_a_type_this_layer_cannot_publish_is_refused_by_name():
    @tool("stub_odd", needs=(READS,))
    def stub_odd(j: Job, when: Annotated[dict, "A dict, which no tool takes."]) -> None:
        """A tool with a parameter this layer does not publish."""

    with pytest.raises(schema.SchemaError) as caught:
        schema.tool_schema(stub_odd)
    assert "stub_odd" in str(caught.value) and "when" in str(caught.value)


def test_a_tool_with_no_docstring_is_refused():
    @tool("stub_mute", needs=(READS,))
    def stub_mute(j: Job) -> None:
        pass

    with pytest.raises(schema.SchemaError) as caught:
        schema.tool_schema(stub_mute)
    assert "stub_mute" in str(caught.value)


# -- what a harness is handed ------------------------------------------------------------------

def test_describe_reports_google_and_writing_per_tool():
    local = schema.describe(stub_inspect)["effects"]
    assert local["google"] is False and local["writes"] is False
    assert local["approval"] == "none"

    written = schema.describe(stub_local_write)["effects"]
    assert written["google"] is False and written["writes"] is True
    assert written["writes_google"] is False and written["approval"] == "recommended"

    google = schema.describe(stub_sync)
    assert google["needs"] == [READS, WRITES, READS_GOOGLE, WRITES_GOOGLE]
    assert google["effects"]["google"] is True
    assert google["effects"]["writes_google"] is True
    assert google["effects"]["approval"] == "required"


def test_a_google_reader_counts_as_touching_google_without_writing():
    @tool("stub_fetch", needs=(READS, READS_GOOGLE))
    def stub_fetch(j: Job) -> None:
        """Read a deck and change nothing."""

    e = schema.describe(stub_fetch)["effects"]
    assert (e["google"], e["reads_google"], e["writes"], e["approval"]) == (True, True, False, "none")


def test_all_schemas_walks_a_supplied_registry_in_its_own_order():
    names = [s["name"] for s in schema.all_schemas(STUBS)]
    assert names == list(STUBS)


def test_the_anthropic_shape_is_name_description_input_schema():
    published = schema.anthropic_tools(STUBS)
    assert {k for t in published for k in t} == {"name", "description", "input_schema"}
    first = published[0]
    assert first["name"] == "stub_inspect"
    assert first["input_schema"]["required"] == ["pdf"]


def test_the_openai_shape_wraps_the_same_schema_as_parameters():
    published = schema.openai_tools(STUBS)
    one = published[0]
    assert one["type"] == "function"
    assert set(one["function"]) == {"name", "description", "parameters"}
    assert one["function"]["parameters"] == schema.tool_schema(stub_inspect)["input_schema"]


def test_the_two_shapes_carry_the_same_schemas():
    a = {t["name"]: t["input_schema"] for t in schema.anthropic_tools(STUBS)}
    o = {t["function"]["name"]: t["function"]["parameters"] for t in schema.openai_tools(STUBS)}
    assert a == o


# -- validate ----------------------------------------------------------------------------------

def test_clean_arguments_pass_through_unchanged():
    assert schema.validate(stub_inspect, {"pdf": "talk.pdf", "pages": 7, "deep": True}) == {
        "pdf": "talk.pdf", "pages": 7, "deep": True}


def test_validate_does_not_fill_in_defaults():
    assert schema.validate(stub_inspect, {"pdf": "talk.pdf"}) == {"pdf": "talk.pdf"}


def test_an_explicit_null_is_accepted_where_the_type_allows_it():
    assert schema.validate(stub_inspect, {"pdf": "a.pdf", "out": None})["out"] is None


def test_a_null_is_refused_where_the_type_does_not_allow_it():
    with pytest.raises(Refused) as caught:
        schema.validate(stub_inspect, {"pdf": None})
    assert caught.value.code == "bad_request"


def test_an_unknown_key_is_refused_and_the_real_ones_are_named():
    with pytest.raises(Refused) as caught:
        schema.validate(stub_inspect, {"pdf": "a.pdf", "pdfs": "b.pdf"})
    assert caught.value.code == "bad_request"
    assert "pdfs" in str(caught.value) and "pdf" in str(caught.value)
    assert caught.value.data["unknown"] == ["pdfs"]


def test_a_missing_required_argument_is_refused_by_name():
    with pytest.raises(Refused) as caught:
        schema.validate(stub_inspect, {"pages": 2})
    assert caught.value.code == "bad_request" and caught.value.data["missing"] == ["pdf"]


def test_no_arguments_at_all_is_a_missing_required_argument():
    with pytest.raises(Refused) as caught:
        schema.validate(stub_inspect, None)
    assert caught.value.data["missing"] == ["pdf"]


@pytest.mark.parametrize("arguments, parameter", [
    ({"pdf": 4}, "pdf"),
    ({"pdf": "a.pdf", "pages": "three"}, "pages"),
    ({"pdf": "a.pdf", "pages": True}, "pages"),        # a bool is not an integer here
    ({"pdf": "a.pdf", "deep": "yes"}, "deep"),
    ({"pdf": "a.pdf", "only": "titles"}, "only"),      # a bare string is not a list
    ({"pdf": "a.pdf", "only": [1]}, "only[0]"),
])
def test_a_value_of_the_wrong_type_is_refused_naming_the_parameter(arguments, parameter):
    with pytest.raises(Refused) as caught:
        schema.validate(stub_inspect, arguments)
    assert caught.value.code == "bad_request"
    assert caught.value.data["parameter"] == parameter


def test_an_integral_float_becomes_an_integer():
    """JSON has one number type, and a client that round-trips 3 can send 3.0 back."""
    assert schema.validate(stub_inspect, {"pdf": "a.pdf", "pages": 3.0})["pages"] == 3
    with pytest.raises(Refused):
        schema.validate(stub_inspect, {"pdf": "a.pdf", "pages": 3.5})


# -- dispatch ----------------------------------------------------------------------------------

def test_dispatch_runs_a_tool_and_returns_its_result(ctx):
    r = mcp.dispatch(ctx, "stub_inspect", {"pdf": "talk.pdf", "pages": 9}, STUBS)
    assert r.ok and r.tool == "stub_inspect"
    assert r.data["pdf"] == "talk.pdf" and r.data["pages"] == 9
    assert r.data["deep"] is False and r.data["out"] is None      # the body's own defaults


def test_dispatch_refuses_an_unknown_tool_without_raising(ctx):
    r = mcp.dispatch(ctx, "deck_inspekt", {"pdf": "a.pdf"}, STUBS)
    assert not r.ok and r.code == "bad_request"
    assert "stub_inspect" in r.summary
    assert r.data["tools"] == sorted(STUBS)


def test_dispatch_refuses_bad_arguments_without_raising(ctx):
    r = mcp.dispatch(ctx, "stub_inspect", {"pdf": "a.pdf", "nope": 1}, STUBS)
    assert not r.ok and r.code == "bad_request" and "nope" in r.summary

    missing = mcp.dispatch(ctx, "stub_inspect", {}, STUBS)
    assert not missing.ok and missing.code == "bad_request"


def test_dispatch_passes_a_tools_own_refusal_through(ctx):
    r = mcp.dispatch(ctx, "stub_refuses", {"deck": "talk"}, STUBS)
    assert not r.ok and r.code == "deck_edited" and r.data["slides"] == [4]


def test_dispatch_is_gated_by_what_the_context_allows(tmp_path):
    """A workspace with no account says `offline`; one that withholds permission, `forbidden`."""
    r = mcp.dispatch(AgentContext.offline(tmp_path), "stub_sync", {"pdf": "a.pdf", "deck": "x"},
                     STUBS)
    assert not r.ok and r.code == "offline"

    withheld = AgentContext(workspace=LocalWorkspace(tmp_path), google=_Available(),
                            allow=LOCAL_ONLY)
    r = mcp.dispatch(withheld, "stub_sync", {"pdf": "a.pdf", "deck": "x"}, STUBS)
    assert not r.ok and r.code == "forbidden"


def test_a_refused_result_is_an_mcp_error_with_the_whole_result_in_it(ctx):
    r = mcp.dispatch(ctx, "stub_refuses", {"deck": "talk"}, STUBS)
    blocks, is_error = mcp.tool_response(r)
    assert is_error is True
    assert blocks[0]["type"] == "text" and "deck_edited" in blocks[0]["text"]

    ok_blocks, no_error = mcp.tool_response(mcp.dispatch(ctx, "stub_local_write", {}, STUBS))
    assert no_error is False and "stub_local_write" in ok_blocks[0]["text"]


# -- the server's own setup --------------------------------------------------------------------

def test_the_context_a_server_builds_honours_read_only_and_offline(tmp_path):
    plain = mcp.build_context(tmp_path)
    assert plain.workspace.root == tmp_path.resolve()
    assert plain.permits(WRITES_GOOGLE)

    read = mcp.build_context(tmp_path, read_only=True)
    assert read.allow == READ_ONLY and not read.permits(WRITES)

    off = mcp.build_context(tmp_path, offline=True)
    assert not off.permits(READS_GOOGLE) and off.google.describe()["available"] is False


def test_the_root_falls_back_to_the_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("B2S_AGENT_ROOT", str(tmp_path))
    assert mcp.build_context().workspace.root == tmp_path.resolve()


def test_instructions_are_there_even_before_the_registry_is():
    said = mcp.instructions(STUBS)
    assert "beamer2slides" in said and len(said) > 80


class _Thing:
    """Any of the SDK's models: what it was handed, and nothing else."""

    def __init__(self, **kw):
        self.__dict__.update(kw)


class _ServerV1:
    """The 1.x low-level server: the handlers arrive through decorators."""

    made: list = []

    def __init__(self, name, instructions=None):
        self.name, self.instructions, self.handlers = name, instructions, {}
        self.request_context = None                            # asked outside a request
        _ServerV1.made.append(self)

    def _take(self, kind):
        def deco(fn):
            self.handlers[kind] = fn
            return fn
        return deco

    def list_tools(self):
        return self._take("list_tools")

    def call_tool(self):
        return self._take("call_tool")

    def list_resources(self):
        return self._take("list_resources")

    def read_resource(self):
        return self._take("read_resource")

    def create_initialization_options(self):
        return {}


class _ServerV2:
    """The 2.x low-level server: the handlers are constructor arguments, and there is no
    `list_tools` on the class at all - which is how `serve` tells the two apart."""

    made: list = []

    def __init__(self, name, *, instructions=None, on_list_tools=None, on_call_tool=None,
                 on_list_resources=None, on_read_resource=None):
        self.name, self.instructions = name, instructions
        self.handlers = {"list_tools": on_list_tools, "call_tool": on_call_tool,
                         "list_resources": on_list_resources, "read_resource": on_read_resource}
        _ServerV2.made.append(self)

    def create_initialization_options(self):
        return {}


def _fake_sdk(monkeypatch, Server):
    """`mcp` and `anyio` enough for `serve` to wire itself up, with neither installed.

    `anyio.run` records the coroutine rather than running it: what is being tested is the wiring,
    so the test then calls the handlers itself.
    """
    models = ["Tool", "Resource", "TextContent", "ListToolsResult", "CallToolResult",
              "ListResourcesResult", "ReadResourceResult", "TextResourceContents"]
    types_mod = ModuleType("mcp.types")
    for name in models:
        setattr(types_mod, name, type(name, (_Thing,), {}))
    sdk = ModuleType("mcp")
    sdk.types = types_mod
    server_mod = ModuleType("mcp.server")
    lowlevel = ModuleType("mcp.server.lowlevel")
    lowlevel.Server = Server
    stdio = ModuleType("mcp.server.stdio")
    stdio.stdio_server = None                                  # never reached: `run` is not run

    class _ToThread:
        @staticmethod
        async def run_sync(fn):
            return fn()

    anyio_mod = ModuleType("anyio")
    anyio_mod.to_thread = _ToThread
    anyio_mod.from_thread = _Thing(run=lambda *a, **k: None)
    served: list = []
    anyio_mod.run = served.append
    for name, module in [("mcp", sdk), ("mcp.types", types_mod), ("mcp.server", server_mod),
                         ("mcp.server.lowlevel", lowlevel), ("mcp.server.stdio", stdio),
                         ("anyio", anyio_mod)]:
        monkeypatch.setitem(sys.modules, name, module)
    return served


def test_the_server_speaks_to_either_generation_of_the_sdk(tmp_path, monkeypatch):
    # The SDK's 2.x took the decorators away and takes the handlers in the constructor instead,
    # so `pip install beamer2slides[mcp]` fetched an SDK this server died on. Both shapes are
    # wired here from fakes, since the real one is an optional extra the suite cannot count on.
    for Server, v1 in ((_ServerV1, True), (_ServerV2, False)):
        Server.made = []
        served = _fake_sdk(monkeypatch, Server)
        mcp.serve(tmp_path, offline=True, tools=STUBS)
        assert served, "the server was built but never run"
        [server] = Server.made
        assert "beamer2slides" in server.instructions       # the rules travel with the tools
        handlers = server.handlers
        assert set(handlers) == {"list_tools", "call_tool", "list_resources", "read_resource"}
        assert all(handlers.values())

        listed = asyncio.run(handlers["list_tools"]() if v1 else handlers["list_tools"](None, None))
        tools = listed if v1 else listed.tools
        assert sorted(t.name for t in tools) == sorted(STUBS)
        assert all(getattr(t, "inputSchema" if v1 else "input_schema")["type"] == "object"
                   for t in tools)

        held = asyncio.run(handlers["read_resource"](mcp.INSTRUCTIONS_URI) if v1 else
                           handlers["read_resource"](None, _Thing(uri=mcp.INSTRUCTIONS_URI)))
        assert "beamer2slides" in (held if v1 else held.contents[0].text)
        with pytest.raises(ValueError):
            asyncio.run(handlers["read_resource"]("b2s://nowhere") if v1 else
                        handlers["read_resource"](None, _Thing(uri="b2s://nowhere")))

        def call(name, arguments):
            if v1:
                return asyncio.run(handlers["call_tool"](name, arguments))
            return asyncio.run(handlers["call_tool"](None, _Thing(name=name,
                                                                  arguments=arguments)))

        good = call("stub_local_write", {})
        blocks = good if v1 else good.content
        assert "stub_local_write" in blocks[0].text
        assert v1 or good.is_error is False

        # A refusal is `isError` and carries the whole Result, whichever way the SDK says it.
        if v1:
            with pytest.raises(Exception) as caught:
                call("stub_refuses", {"deck": "talk"})
            assert "deck_edited" in str(caught.value)
        else:
            bad = call("stub_refuses", {"deck": "talk"})
            assert bad.is_error is True and "deck_edited" in bad.content[0].text


def test_serving_without_the_sdk_says_how_to_install_it(tmp_path):
    try:
        import mcp as _sdk                                       # noqa: F401
    except ImportError:
        pass
    else:
        pytest.skip("the MCP SDK is installed, so serve() would try to run")
    with pytest.raises(mcp.MissingSDK) as caught:
        mcp.serve(tmp_path, tools=STUBS)
    assert "pip install beamer2slides[mcp]" in str(caught.value)
