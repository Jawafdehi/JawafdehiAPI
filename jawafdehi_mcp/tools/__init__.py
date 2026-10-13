"""Tool implementations for Jawafdehi MCP server."""

from .base import BaseTool, ToolExecutionResult, error_text
from .control_plane import (
    BrowseCourtDataTool,
    BrowseMaterialsTool,
    DeleteNESEntityTool,
    GetNESEntityVersionsTool,
    ManageCaseUpdateProposalsTool,
    ManageCaseworkReviewsTool,
    ManageCourtDataTool,
    ManageJobsTool,
    ManageMaterialTool,
    SearchControlPlaneTool,
)
from .date_converter import DateConverterTool
from .document_converter import DocumentConverterTool
from .jawafdehi_cases import (
    CreateJawafdehiCaseTool,
    DeleteJawafdehiCaseTool,
    GetJawafdehiCaseTool,
    PatchJawafdehiCaseTool,
    SearchJawafdehiCasesTool,
    SubmitNESChangeTool,
    UploadMaterialFileTool,
)
from .nes import (
    GetNESEntitiesTool,
    GetNESEntityPrefixesTool,
    GetNESTagsTool,
    SearchNESEntitiesTool,
)
from .ngm_extract import NGMExtractCaseDataTool
from .ngm_judicial import NGMJudicialTool
from .whoami import GetCurrentUserTool

__all__ = [
    "BaseTool",
    "ToolExecutionResult",
    "error_text",
    "SearchControlPlaneTool",
    "GetNESEntityVersionsTool",
    "DeleteNESEntityTool",
    "BrowseMaterialsTool",
    "ManageMaterialTool",
    "BrowseCourtDataTool",
    "ManageCourtDataTool",
    "ManageCaseUpdateProposalsTool",
    "ManageCaseworkReviewsTool",
    "ManageJobsTool",
    "GetCurrentUserTool",
    "NGMJudicialTool",
    "NGMExtractCaseDataTool",
    "SearchJawafdehiCasesTool",
    "GetJawafdehiCaseTool",
    "CreateJawafdehiCaseTool",
    "DeleteJawafdehiCaseTool",
    "PatchJawafdehiCaseTool",
    "SubmitNESChangeTool",
    "UploadMaterialFileTool",
    "SearchNESEntitiesTool",
    "GetNESEntitiesTool",
    "GetNESEntityPrefixesTool",
    "GetNESTagsTool",
    "DateConverterTool",
    "DocumentConverterTool",
]
