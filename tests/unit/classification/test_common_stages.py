from __future__ import annotations

from io import BytesIO
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from starlette.datastructures import UploadFile

from src.classification.model import ModelNotAvailableError
from src.classification.stages.filename import stage_filename
from src.classification.stages.metadata import stage_metadata
from src.classification.stages.ocr import stage_ocr
from src.classification.stages.text import stage_text
from src.classification.types import StageOutcome
from src.core.exceptions import MetadataProcessingError
from pdfminer.pdfparser import PDFSyntaxError
from pdfminer.psparser import PSException
from pdfminer.pdfdocument import PDFTextExtractionNotAllowed
from pdfminer.pdftypes import PDFException


@pytest.fixture
def mock_upload_file_factory():
    """Factory to create mock UploadFile objects for testing stages."""

    def _factory(
        filename: str, content: bytes, content_type: str | None = None
    ) -> MagicMock:
        mock_file = MagicMock(spec=UploadFile)
        mock_file.filename = filename
        mock_file.content_type = content_type

        # Mock the file-like object within UploadFile
        mock_file.file = BytesIO(content)  # Use BytesIO for seek/read
        # For stages that might use async seek/read on UploadFile itself
        mock_file.seek = AsyncMock()
        mock_file.read = AsyncMock(return_value=content)
        return mock_file

    return _factory


# Test Filename Stage
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "filename, expected_label, expected_confidence_range",
    [
        ("invoice_123.pdf", "invoice", (0.80, 0.95)),
        ("my_bank_statement.docx", "bank_statement", (0.80, 0.95)),
        ("financial_report_final.xlsx", "financial_report", (0.80, 0.95)),
        ("drivers_license_scan.jpg", "drivers_licence", (0.80, 0.95)),
        ("id_card_john_doe.png", "id_doc", (0.80, 0.95)),
        ("service_agreement.pdf", "contract", (0.80, 0.95)),
        ("important_email.eml", "email", (0.80, 0.95)),  # .eml specific check
        ("application_form_v2.pdf", "form", (0.80, 0.95)),
        ("unknown_document.dat", None, None),
        ("", None, None),  # Empty filename
        (None, None, None),  # None filename
        ("path/to/invoice.pdf", "invoice", (0.80, 0.95)),  # With path
        ("INV001.pdf", "invoice", (0.80, 0.95)),  # Strong start
    ],
)
async def test_stage_filename(
    filename: str | None,  # Allow None
    expected_label: str | None,
    expected_confidence_range: tuple[float, float] | None,
    mock_upload_file_factory,
) -> None:
    """Tests the filename stage with various inputs."""
    # Handle None filename case for factory
    if filename is None:
        mock_file = mock_upload_file_factory(
            "dummy", b"dummy", "application/octet-stream"
        )
        mock_file.filename = None  # Explicitly set to None after creation
    else:
        mock_file = mock_upload_file_factory(
            filename, b"dummy", "application/octet-stream"
        )

    outcome = await stage_filename(mock_file)

    assert outcome.label == expected_label
    if expected_confidence_range and outcome.confidence is not None:
        assert (
            expected_confidence_range[0]
            <= outcome.confidence
            <= expected_confidence_range[1]
        )
    else:
        assert outcome.confidence is None


# Test Metadata Stage
@pytest.mark.asyncio
async def test_stage_metadata_pdf_match(mock_upload_file_factory) -> None:
    """Tests metadata stage with a PDF that has matching metadata."""
    mock_file = mock_upload_file_factory(
        "meta_invoice.pdf", b"pdf_content", "application/pdf"
    )

    with patch(
        "src.classification.stages.metadata._extract_pdf_metadata",
        AsyncMock(return_value="This is an Invoice"),
    ) as mock_extract:
        outcome = await stage_metadata(mock_file)
        # Ensure _extract_pdf_metadata is called with content AND filename
        mock_extract.assert_called_once_with(b"pdf_content", "meta_invoice.pdf")
        assert outcome.label == "invoice"
        assert outcome.confidence == pytest.approx(0.86)


@pytest.mark.asyncio
async def test_stage_metadata_pdf_no_match(mock_upload_file_factory) -> None:
    """Tests metadata stage with a PDF that has no matching metadata."""
    mock_file = mock_upload_file_factory(
        "other_doc.pdf", b"pdf_content", "application/pdf"
    )
    with patch(
        "src.classification.stages.metadata._extract_pdf_metadata",
        AsyncMock(return_value="Generic document info"),
    ) as mock_extract:
        outcome = await stage_metadata(mock_file)
        # Ensure _extract_pdf_metadata is called with content AND filename
        mock_extract.assert_called_once_with(b"pdf_content", "other_doc.pdf")
        assert outcome.label is None
        assert outcome.confidence is None


@pytest.mark.asyncio
async def test_stage_metadata_not_pdf(mock_upload_file_factory) -> None:
    """Tests metadata stage with a non-PDF file, should skip."""
    mock_file = mock_upload_file_factory("document.txt", b"text_content", "text/plain")
    with patch(
        "src.classification.stages.metadata._extract_pdf_metadata",
        new_callable=AsyncMock,
    ) as mock_extract:
        outcome = await stage_metadata(mock_file)
        mock_extract.assert_not_called()
        assert outcome.label is None
        assert outcome.confidence is None


@pytest.mark.asyncio
async def test_stage_metadata_pdf_extraction_fails(mock_upload_file_factory) -> None:
    """Tests metadata stage when PDF metadata extraction returns empty string (simulating failure)."""
    mock_file = mock_upload_file_factory("corrupt.pdf", b"bad_pdf", "application/pdf")
    with patch(
        "src.classification.stages.metadata._extract_pdf_metadata",
        AsyncMock(return_value=""),
    ) as mock_extract:
        outcome = await stage_metadata(mock_file)
        # Ensure _extract_pdf_metadata is called with content AND filename
        mock_extract.assert_called_once_with(b"bad_pdf", "corrupt.pdf")
        assert outcome.label is None
        assert outcome.confidence is None


@pytest.mark.asyncio
async def test_stage_metadata_processing_error(mock_upload_file_factory) -> None:
    """Tests metadata stage handles generic exception during processing by raising MetadataProcessingError."""
    mock_file = mock_upload_file_factory("error.pdf", b"pdf_content", "application/pdf")
    # Mock file read to raise an exception, which should be wrapped in MetadataProcessingError
    mock_file.read = AsyncMock(side_effect=OSError("Simulated read error"))

    with patch("src.classification.stages.metadata.logger") as mock_logger:
        with pytest.raises(MetadataProcessingError) as excinfo:
            await stage_metadata(mock_file)

        assert "File I/O error in metadata stage: Simulated read error" in str(
            excinfo.value
        )
        # Check that the error was logged
        mock_logger.error.assert_called_once_with(
            "metadata_stage_io_error",  # Updated to match the new specific log event
            filename="error.pdf",
            error="Simulated read error",
            exc_info=True,
        )

    # Test with a generic exception from _extract_pdf_metadata
    mock_file.read = AsyncMock(return_value=b"pdf_content")  # Reset read mock
    with (
        patch(
            "src.classification.stages.metadata._extract_pdf_metadata",
            AsyncMock(side_effect=Exception("Internal extraction boom")),
        ) as mock_extract_boom,
        patch("src.classification.stages.metadata.logger") as mock_logger_boom,
    ):
        with pytest.raises(MetadataProcessingError) as excinfo_boom:
            await stage_metadata(mock_file)
        assert (
            "General processing error in metadata stage: Internal extraction boom"
            in str(excinfo_boom.value)
        )
        mock_extract_boom.assert_called_once_with(b"pdf_content", "error.pdf")
        # This log comes from the except Exception block in stage_metadata
        mock_logger_boom.error.assert_called_once_with(
            "metadata_stage_processing_error",
            filename="error.pdf",
            error="Internal extraction boom",
            exc_info=True,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "exception_type",
    [
        PDFSyntaxError("bad syntax"),
        PSException("postscript error"),
        PDFException("pdf issue"),
        PDFTextExtractionNotAllowed("extraction not allowed"),
        Exception("generic worker error"),
    ],
)
async def test_stage_metadata_pdf_extraction_worker_errors(
    mock_upload_file_factory,
    exception_type: Exception,
) -> None:
    """Tests the worker function inside _extract_pdf_metadata handles specific PDF errors."""
    mock_file = mock_upload_file_factory(
        "worker_error.pdf", b"bad_pdf", "application/pdf"
    )

    # We need to patch the *actual* pdfminer function called by the worker
    with (
        patch(
            "src.classification.stages.metadata.extract_text",
            side_effect=exception_type,
        ) as mock_pdfminer_extract,
        patch("src.classification.stages.metadata.logger") as mock_logger,
    ):
        if isinstance(exception_type, Exception) and not isinstance(
            exception_type,
            (PDFSyntaxError, PSException, PDFException, PDFTextExtractionNotAllowed),
        ):
            # For generic exceptions, the worker raises MetadataProcessingError
            with pytest.raises(MetadataProcessingError) as excinfo:
                await stage_metadata(mock_file)
            assert (
                "Unexpected error in PDF metadata worker: generic worker error"
                in str(excinfo.value)
            )
            mock_logger.error.assert_called_once_with(
                "pdf_metadata_extraction_unexpected_error",
                filename="worker_error.pdf",
                error="generic worker error",
                exc_info=True,
            )
        else:
            # For specific PDF errors, the worker logs a warning and returns empty string
            outcome = await stage_metadata(mock_file)
            assert outcome.label is None
            assert outcome.confidence is None

            # Check that the specific pdfminer function was called inside the worker thread
            mock_pdfminer_extract.assert_called_once()

            # Check for appropriate warning log based on exception type
            if isinstance(exception_type, PDFTextExtractionNotAllowed):
                mock_logger.warning.assert_called_once_with(
                    "pdf_metadata_extraction_denied", filename="worker_error.pdf"
                )
            else:
                mock_logger.warning.assert_called_once_with(
                    "pdf_metadata_extraction_failed_pdfminer",
                    filename="worker_error.pdf",
                    error=str(exception_type),
                    error_type=type(exception_type).__name__,
                )


@pytest.mark.asyncio
async def test_stage_metadata_pdf_empty_or_whitespace_metadata(
    mock_upload_file_factory,
) -> None:
    """Tests metadata stage handles empty or whitespace-only metadata."""
    mock_file = mock_upload_file_factory(
        "empty_meta.pdf", b"pdf_content", "application/pdf"
    )

    for metadata_value in ["", "   \n "]:
        with patch(
            "src.classification.stages.metadata._extract_pdf_metadata",
            AsyncMock(return_value=metadata_value),
        ) as mock_extract:
            outcome = await stage_metadata(mock_file)
            mock_extract.assert_called_once_with(b"pdf_content", "empty_meta.pdf")
            assert outcome.label is None
            assert outcome.confidence is None


@pytest.mark.asyncio
async def test_stage_metadata_reraises_metadata_processing_error_from_worker(
    mock_upload_file_factory,
) -> None:
    """Test that stage_metadata correctly re-raises MetadataProcessingError from worker."""
    mock_file = mock_upload_file_factory(
        "reraise.pdf", b"pdf_content", "application/pdf"
    )
    worker_exception = MetadataProcessingError("Worker-specific processing error")

    with (
        patch(
            "src.classification.stages.metadata._extract_pdf_metadata",
            AsyncMock(side_effect=worker_exception),
        ) as mock_extract,
        patch("src.classification.stages.metadata.logger") as mock_logger,
    ):  # Mock logger to ensure it's NOT called for this re-raise path
        with pytest.raises(MetadataProcessingError) as excinfo:
            await stage_metadata(mock_file)

        assert (
            excinfo.value is worker_exception
        )  # Ensure the exact exception is re-raised
        mock_extract.assert_called_once_with(b"pdf_content", "reraise.pdf")
        mock_logger.error.assert_not_called()  # No new error should be logged by stage_metadata
        mock_logger.warning.assert_not_called()


# Test Text Stage
@pytest.mark.asyncio
async def test_stage_text_with_model(mock_upload_file_factory) -> None:
    """Tests text stage when ML model is available and predicts."""
    mock_file = mock_upload_file_factory("invoice.pdf", b"content", "application/pdf")
    mock_pdf_parser = AsyncMock(return_value="extracted invoice text")

    # Patch the TEXT_EXTRACTORS within the text stage module
    # Patch the imported 'predict' function within the text stage module
    with (
        patch.dict(
            "src.classification.stages.text.TEXT_EXTRACTORS", {"pdf": mock_pdf_parser}
        ),
        patch("src.classification.stages.text._MODEL_AVAILABLE", True),
        patch(
            "src.classification.stages.text.predict",
            return_value=("invoice_model", 0.88),
        ) as mock_model_predict,
        patch("src.classification.stages.text.logger") as mock_logger,
    ):
        outcome = await stage_text(mock_file)

        mock_file.seek.assert_called_once_with(0)
        mock_pdf_parser.assert_called_once_with(mock_file)
        mock_model_predict.assert_called_once_with("extracted invoice text")
        assert outcome.label == "invoice_model"
        assert outcome.confidence == pytest.approx(0.88)
        mock_logger.debug.assert_any_call(
            "text_stage_model_prediction",
            filename="invoice.pdf",
            label="invoice_model",
            confidence=0.88,
        )


@pytest.mark.asyncio
async def test_stage_text_model_unavailable_fallback_heuristic(
    mock_upload_file_factory,
) -> None:
    """Tests text stage fallback to heuristics when model is unavailable."""
    mock_file = mock_upload_file_factory("statement.csv", b"content", "text/csv")
    mock_csv_parser = AsyncMock(return_value="bank statement keywords here")

    # Patch 'predict' to raise ModelNotAvailableError
    with (
        patch.dict(
            "src.classification.stages.text.TEXT_EXTRACTORS", {"csv": mock_csv_parser}
        ),
        patch(
            "src.classification.stages.text._MODEL_AVAILABLE", True
        ),  # Model is available
        patch(
            "src.classification.stages.text.predict",
            side_effect=ModelNotAvailableError("Model not found"),
        ) as mock_model_predict,
        patch("src.classification.stages.text.logger") as mock_logger,
    ):
        outcome = await stage_text(mock_file)

        mock_file.seek.assert_called_once_with(0)
        mock_csv_parser.assert_called_once_with(mock_file)
        mock_model_predict.assert_called_once_with(
            "bank statement keywords here"
        )  # Check predict was called
        mock_logger.warning.assert_called_once_with(
            "text_stage_model_not_available",
            filename="statement.csv",
            fallback="heuristics",
        )
        mock_logger.debug.assert_any_call(  # Check heuristic match logging
            "text_stage_heuristic_match",
            filename="statement.csv",
            label="bank_statement",
            confidence=0.75,
        )
        assert outcome.label == "bank_statement"  # From heuristic
        assert outcome.confidence == pytest.approx(0.75)  # Fallback confidence


@pytest.mark.asyncio
async def test_stage_text_unsupported_extension(mock_upload_file_factory) -> None:
    """Tests text stage with an unsupported text file extension."""
    mock_file = mock_upload_file_factory("archive.zip", b"content", "application/zip")
    # Ensure TEXT_EXTRACTORS doesn't have 'zip' by patching it (or ensure default doesn't)
    with patch.dict("src.classification.stages.text.TEXT_EXTRACTORS", {}, clear=True):
        outcome = await stage_text(mock_file)
        assert outcome.label is None
        assert outcome.confidence is None


@pytest.mark.asyncio
async def test_stage_text_empty_extracted_text(mock_upload_file_factory) -> None:
    """Tests text stage when the parser returns empty text."""
    mock_file = mock_upload_file_factory("empty.txt", b"", "text/plain")
    mock_txt_parser = AsyncMock(return_value="  ")  # Whitespace only

    with patch.dict(
        "src.classification.stages.text.TEXT_EXTRACTORS", {"txt": mock_txt_parser}
    ):
        outcome = await stage_text(mock_file)
        mock_file.seek.assert_called_once_with(0)
        mock_txt_parser.assert_called_once_with(mock_file)
        assert outcome.label is None
        assert outcome.confidence is None


@pytest.mark.asyncio
async def test_stage_text_extraction_error(mock_upload_file_factory) -> None:
    """Tests text stage handling of generic exception during text extraction."""
    mock_file = mock_upload_file_factory("error.txt", b"content", "text/plain")
    mock_txt_parser = AsyncMock(side_effect=Exception("Simulated extraction error"))

    with (
        patch.dict(
            "src.classification.stages.text.TEXT_EXTRACTORS", {"txt": mock_txt_parser}
        ),
        patch("src.classification.stages.text.logger") as mock_logger,
    ):
        outcome = await stage_text(mock_file)

        assert outcome.label is None
        assert outcome.confidence is None
        mock_logger.error.assert_called_once_with(
            "text_stage_extraction_error",
            filename="error.txt",
            extension="txt",
            error="Simulated extraction error",
            exc_info=True,
        )


@pytest.mark.asyncio
async def test_stage_text_model_prediction_error(mock_upload_file_factory) -> None:
    """Tests text stage handling of generic exception during model prediction."""
    mock_file = mock_upload_file_factory("predict_error.txt", b"content", "text/plain")
    mock_txt_parser = AsyncMock(return_value="some text")

    with (
        patch.dict(
            "src.classification.stages.text.TEXT_EXTRACTORS", {"txt": mock_txt_parser}
        ),
        patch("src.classification.stages.text._MODEL_AVAILABLE", True),
        patch(
            "src.classification.stages.text.predict",
            side_effect=Exception("Simulated prediction error"),
        ) as mock_model_predict,
        patch("src.classification.stages.text.logger") as mock_logger,
    ):
        outcome = await stage_text(mock_file)

        assert outcome.label is None
        assert outcome.confidence is None
        mock_model_predict.assert_called_once_with("some text")
        mock_logger.error.assert_called_once_with(
            "text_stage_model_prediction_error",
            filename="predict_error.txt",
            error="Simulated prediction error",
            exc_info=True,
        )


@pytest.mark.asyncio
async def test_stage_text_model_returns_none_fallback_no_heuristic_match(
    mock_upload_file_factory,
) -> None:
    """Text stage: model returns (None, None), no heuristic match."""
    mock_file = mock_upload_file_factory("nomatch.txt", b"content", "text/plain")
    mock_txt_parser = AsyncMock(return_value="unique text no keywords")

    with (
        patch.dict(
            "src.classification.stages.text.TEXT_EXTRACTORS", {"txt": mock_txt_parser}
        ),
        patch("src.classification.stages.text._MODEL_AVAILABLE", True),
        patch(
            "src.classification.stages.text.predict", return_value=(None, None)
        ) as mock_model_predict,  # Model returns no prediction
        patch("src.classification.stages.text.logger") as mock_logger,
    ):
        outcome = await stage_text(mock_file)

        assert outcome.label is None
        assert outcome.confidence is None
        mock_model_predict.assert_called_once_with("unique text no keywords")
        mock_logger.debug.assert_any_call(
            "text_stage_model_no_prediction",
            filename="nomatch.txt",
            text_preview="unique text no keywords"[:100],
        )
        mock_logger.debug.assert_any_call(
            "text_stage_no_match",
            filename="nomatch.txt",
            text_preview="unique text no keywords"[:100],
        )


# Test OCR Stage
@pytest.mark.asyncio
async def test_stage_ocr_with_model(mock_upload_file_factory) -> None:
    """Tests OCR stage when ML model is available and predicts."""
    mock_file = mock_upload_file_factory("license.png", b"img_content", "image/png")
    mock_image_parser = AsyncMock(return_value="ocr text drivers license")

    # Patch the IMAGE_EXTRACTORS within the ocr stage module
    # Patch the imported 'predict' function within the ocr stage module
    with (
        patch.dict(
            "src.classification.stages.ocr.IMAGE_EXTRACTORS", {"png": mock_image_parser}
        ),
        patch("src.classification.stages.ocr._MODEL_AVAILABLE", True),
        patch(
            "src.classification.stages.ocr.predict",
            return_value=("drivers_licence_model", 0.91),
        ) as mock_model_predict,
        patch("src.classification.stages.ocr.logger") as mock_logger,
    ):
        outcome = await stage_ocr(mock_file)

        mock_file.seek.assert_called_once_with(0)
        mock_image_parser.assert_called_once_with(mock_file)
        mock_model_predict.assert_called_once_with("ocr text drivers license")
        assert outcome.label == "drivers_licence_model"
        assert outcome.confidence == pytest.approx(0.91)
        mock_logger.debug.assert_any_call(
            "ocr_stage_model_prediction",
            filename="license.png",
            label="drivers_licence_model",
            confidence=0.91,
        )


@pytest.mark.asyncio
async def test_stage_ocr_model_unavailable_fallback_heuristic(
    mock_upload_file_factory,
) -> None:
    """Tests OCR stage fallback to heuristics when model is unavailable."""
    mock_file = mock_upload_file_factory("photo_id.jpg", b"img_content", "image/jpeg")
    mock_image_parser = AsyncMock(return_value="some form application text")

    # Patch 'predict' to raise ModelNotAvailableError
    with (
        patch.dict(
            "src.classification.stages.ocr.IMAGE_EXTRACTORS", {"jpg": mock_image_parser}
        ),
        patch(
            "src.classification.stages.ocr._MODEL_AVAILABLE", True
        ),  # Model is available
        patch(
            "src.classification.stages.ocr.predict",
            side_effect=ModelNotAvailableError("Model not found"),
        ) as mock_model_predict,
        patch("src.classification.stages.ocr.logger") as mock_logger,
    ):
        outcome = await stage_ocr(mock_file)

        mock_file.seek.assert_called_once_with(0)
        mock_image_parser.assert_called_once_with(mock_file)
        mock_model_predict.assert_called_once_with(
            "some form application text"
        )  # Check predict was called
        mock_logger.warning.assert_called_once_with(
            "ocr_stage_model_not_available",
            filename="photo_id.jpg",
            fallback="heuristics",
        )
        mock_logger.debug.assert_any_call(  # Check heuristic match logging
            "ocr_stage_heuristic_match",
            filename="photo_id.jpg",
            label="form",
            confidence=0.72,
        )
        assert outcome.label == "form"  # From heuristic
        assert outcome.confidence == pytest.approx(0.72)  # Fallback confidence


@pytest.mark.asyncio
async def test_stage_ocr_unsupported_extension(mock_upload_file_factory) -> None:
    """Tests OCR stage with an unsupported image file extension."""
    mock_file = mock_upload_file_factory(
        "document.pdf", b"pdf_content", "application/pdf"
    )
    # Ensure IMAGE_EXTRACTORS doesn't have 'pdf'
    with patch.dict("src.classification.stages.ocr.IMAGE_EXTRACTORS", {}, clear=True):
        outcome = await stage_ocr(mock_file)
        assert outcome.label is None
        assert outcome.confidence is None


@pytest.mark.asyncio
async def test_stage_ocr_empty_extracted_text(mock_upload_file_factory) -> None:
    """Tests OCR stage when the image parser (OCR) returns empty text."""
    mock_file = mock_upload_file_factory("blank_image.png", b"img_content", "image/png")
    mock_image_parser = AsyncMock(return_value="\n \t ")  # Whitespace only

    with patch.dict(
        "src.classification.stages.ocr.IMAGE_EXTRACTORS", {"png": mock_image_parser}
    ):
        outcome = await stage_ocr(mock_file)
        mock_file.seek.assert_called_once_with(0)
        mock_image_parser.assert_called_once_with(mock_file)
        assert outcome.label is None
        assert outcome.confidence is None


@pytest.mark.asyncio
async def test_stage_ocr_extraction_error(mock_upload_file_factory) -> None:
    """Tests OCR stage handling of generic exception during OCR extraction."""
    mock_file = mock_upload_file_factory("error.jpg", b"img_content", "image/jpeg")
    mock_image_parser = AsyncMock(side_effect=Exception("Simulated OCR error"))

    with (
        patch.dict(
            "src.classification.stages.ocr.IMAGE_EXTRACTORS", {"jpg": mock_image_parser}
        ),
        patch("src.classification.stages.ocr.logger") as mock_logger,
    ):
        outcome = await stage_ocr(mock_file)

        assert outcome.label is None
        assert outcome.confidence is None
        mock_logger.error.assert_called_once_with(
            "ocr_stage_extraction_error",
            filename="error.jpg",
            extension="jpg",
            error="Simulated OCR error",
            exc_info=True,
        )


@pytest.mark.asyncio
async def test_stage_ocr_model_prediction_error(mock_upload_file_factory) -> None:
    """Tests OCR stage handling of generic exception during model prediction."""
    mock_file = mock_upload_file_factory(
        "predict_error.png", b"img_content", "image/png"
    )
    mock_image_parser = AsyncMock(return_value="some ocr text")

    with (
        patch.dict(
            "src.classification.stages.ocr.IMAGE_EXTRACTORS", {"png": mock_image_parser}
        ),
        patch("src.classification.stages.ocr._MODEL_AVAILABLE", True),
        patch(
            "src.classification.stages.ocr.predict",
            side_effect=Exception("Simulated prediction error"),
        ) as mock_model_predict,
        patch("src.classification.stages.ocr.logger") as mock_logger,
    ):
        outcome = await stage_ocr(mock_file)

        assert outcome.label is None
        assert outcome.confidence is None
        mock_model_predict.assert_called_once_with("some ocr text")
        mock_logger.error.assert_called_once_with(
            "ocr_stage_model_prediction_error",
            filename="predict_error.png",
            error="Simulated prediction error",
            exc_info=True,
        )


@pytest.mark.asyncio
async def test_stage_ocr_model_returns_none_fallback_no_heuristic_match(
    mock_upload_file_factory,
) -> None:
    """OCR stage: model returns (None, None), no heuristic match."""
    mock_file = mock_upload_file_factory("nomatch.jpg", b"img_content", "image/jpeg")
    mock_image_parser = AsyncMock(return_value="very unique ocr content")

    with (
        patch.dict(
            "src.classification.stages.ocr.IMAGE_EXTRACTORS", {"jpg": mock_image_parser}
        ),
        patch("src.classification.stages.ocr._MODEL_AVAILABLE", True),
        patch(
            "src.classification.stages.ocr.predict", return_value=(None, None)
        ) as mock_model_predict,  # Model returns no prediction
        patch("src.classification.stages.ocr.logger") as mock_logger,
    ):
        outcome = await stage_ocr(mock_file)

        assert outcome.label is None
        assert outcome.confidence is None
        mock_model_predict.assert_called_once_with("very unique ocr content")
        mock_logger.debug.assert_any_call(
            "ocr_stage_model_no_prediction",
            filename="nomatch.jpg",
            text_preview="very unique ocr content"[:100],
        )
        mock_logger.debug.assert_any_call(
            "ocr_stage_no_match",
            filename="nomatch.jpg",
            text_preview="very unique ocr content"[:100],
        )
