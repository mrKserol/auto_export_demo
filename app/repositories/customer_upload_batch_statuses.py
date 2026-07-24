"""String statuses for customer upload batches (PostgreSQL TEXT, not ENUM)."""

from __future__ import annotations


class CustomerUploadBatchStatus:
    COLLECTING = "collecting"
    RECOGNIZING = "recognizing"
    RECOGNIZED = "recognized"
    CREATING_FOLDER = "creating_folder"
    UPLOADING = "uploading"
    FILES_SAVED = "files_saved"
    AWAITING_CONFIRMATION = "awaiting_confirmation"
    CUSTOMER_SAVED = "customer_saved"
    ABANDONED = "abandoned"
    FAILED = "failed"

    ALL = frozenset(
        {
            COLLECTING,
            RECOGNIZING,
            RECOGNIZED,
            CREATING_FOLDER,
            UPLOADING,
            FILES_SAVED,
            AWAITING_CONFIRMATION,
            CUSTOMER_SAVED,
            ABANDONED,
            FAILED,
        }
    )

    TERMINAL = frozenset(
        {
            CUSTOMER_SAVED,
            ABANDONED,
            FAILED,
        }
    )

    RECOGNITION_RESUMABLE = frozenset(
        {
            COLLECTING,
            RECOGNIZING,
            FAILED,
            RECOGNIZED,
            CREATING_FOLDER,
            UPLOADING,
            FILES_SAVED,
            AWAITING_CONFIRMATION,
        }
    )

    ACTIVE = frozenset(ALL - TERMINAL)


class CustomerUploadBatchFileRecognitionStatus:
    PENDING = "pending"
    PROCESSING = "processing"
    SUCCESS = "success"
    FAILED = "failed"

    NEEDS_RECOGNITION = frozenset(
        {
            PENDING,
            PROCESSING,
            FAILED,
        }
    )
