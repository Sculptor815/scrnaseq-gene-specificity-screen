from __future__ import annotations

import json
from pathlib import Path

import pytest
from mcp import Client
from openpyxl import Workbook

from mcp_server import server


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def hpa_workbook(tmp_path: Path) -> Path:
    path = tmp_path / "HPA_RNA_expression.xlsx"
    workbook = Workbook()
    cells = workbook.active
    cells.title = "RNA_Cell_Types"
    cells.append(["Gene", "Ensembl", "Platelets", "T-cells"])
    cells.append(["PF4", "ENSG1", 100.0, 1.0])
    cells.append(["ACTB", "ENSG2", 10.0, 9.0])
    cells.append(["VWF", "ENSG3", 1.0, 100.0])
    tissues = workbook.create_sheet("RNA_Tissues")
    tissues.append(["Gene", "Ensembl", "bone marrow", "spleen"])
    tissues.append(["PF4", "ENSG1", 20.0, 10.0])
    tissues.append(["ACTB", "ENSG2", 5.0, 6.0])
    tissues.append(["VWF", "ENSG3", 2.0, 8.0])
    workbook.save(path)
    return path


@pytest.fixture
def config(tmp_path: Path) -> Path:
    path = tmp_path / "config.json"
    path.write_text(json.dumps({
        "specificity_groups": {"MK_lineage": ["Platelets"]},
        "reference_tissue": "bone marrow",
        "borderline_fraction": 0.85,
    }), encoding="utf-8")
    return path


@pytest.fixture
def anyio_client(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(server, "RUNS_DIR", tmp_path / "runs")
    return server.mcp


@pytest.mark.anyio
async def test_existing_workbook_screen_and_result_pagination(
    anyio_client, hpa_workbook: Path, config: Path
):
    async with Client(anyio_client, raise_exceptions=True) as client:
        tools = await client.list_tools()
        assert {
            "run_specificity_screen",
            "screen_existing_workbook",
            "get_screening_results",
            "list_available_cell_types",
        } <= {tool.name for tool in tools.tools}

        result = await client.call_tool("screen_existing_workbook", {
            "hpa_excel_path": str(hpa_workbook),
            "config_path": str(config),
        })
        assert not result.is_error
        data = result.structured_content
        assert data["status"] == "completed"
        assert data["selected_gene_count"] == 2
        assert [row["gene"] for row in data["selected_genes"]] == ["PF4", "ACTB"]
        assert data["truncated"] is False
        run_id = data["run_id"]

        page = await client.call_tool("get_screening_results", {
            "run_id": run_id,
            "offset": 0,
            "limit": 1,
        })
        assert page.structured_content["returned_gene_count"] == 1
        assert page.structured_content["truncated"] is True

        last = await client.call_tool("get_screening_results", {
            "run_id": run_id,
            "offset": 1,
            "limit": 1,
        })
        assert last.structured_content["selected_genes"][0]["gene"] == "ACTB"
        assert last.structured_content["truncated"] is False

        catalog = await client.call_tool("list_available_cell_types", {
            "hpa_excel_path": str(hpa_workbook),
        })
        assert catalog.structured_content["cell_types"] == ["Platelets", "T-cells"]


@pytest.mark.anyio
async def test_unknown_cell_type_is_actionable_tool_error(
    anyio_client, hpa_workbook: Path, config: Path
):
    bad_config = config.with_name("bad-config.json")
    bad_config.write_text(json.dumps({
        "specificity_groups": {"typo": ["Not an HPA cell type"]},
    }), encoding="utf-8")
    async with Client(anyio_client, raise_exceptions=True) as client:
        result = await client.call_tool("screen_existing_workbook", {
            "hpa_excel_path": str(hpa_workbook),
            "config_path": str(bad_config),
        })
    assert result.is_error
    assert "Not an HPA cell type" in result.content[0].text
    assert "list_available_cell_types" in result.content[0].text


@pytest.mark.anyio
async def test_invalid_run_id_and_missing_input_return_tool_errors(anyio_client):
    async with Client(anyio_client, raise_exceptions=True) as client:
        invalid_id = await client.call_tool("get_screening_results", {
            "run_id": "../../outside",
        })
        missing = await client.call_tool("run_specificity_screen", {
            "gene_list_path": "does-not-exist.csv",
            "make_plots": False,
        })
        too_many_workers = await client.call_tool("run_specificity_screen", {
            "gene_list_path": "does-not-exist.csv",
            "workers": 99,
        })
    assert invalid_id.is_error
    assert "Invalid run_id" in invalid_id.content[0].text
    assert missing.is_error
    assert "Gene-list file" in missing.content[0].text
    assert too_many_workers.is_error
