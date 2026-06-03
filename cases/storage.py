"""
Custom storage backends for Jawafdehi.

This module contains custom Django storage backends that extend the default
django-storages S3 backend with additional functionality like file name hashing.
"""

import hashlib
import os

from storages.backends.s3boto3 import S3Boto3Storage


class HashedFilenameS3Boto3Storage(S3Boto3Storage):
    """
    S3 storage backend that hashes file names for security and uniqueness.

    This storage backend automatically generates a hash of the original filename
    and uses it as the stored filename, while preserving the original filename
    in a separate field for display purposes.

    The hash is generated using SHA-256 and includes a salt for additional security.
    """

    def __init__(self, *args, **kwargs):
        # Get hash salt from environment or use a default
        self.hash_salt = os.getenv("FILE_HASH_SALT", "jawafdehi-file-salt")
        # Get file prefix from environment or use a default
        self.file_prefix = os.getenv("FILE_STORAGE_PREFIX", "case_uploads/")
        super().__init__(*args, **kwargs)

    def _get_hashed_filename(self, name):
        """
        Generate a hashed filename from the original filename.

        Args:
            name: Original filename

        Returns:
            str: Hashed filename with original extension
        """
        if not name:
            return name

        # Split filename into name and extension
        name_part, ext = os.path.splitext(name)

        # Create hash of the original name with salt
        hash_input = f"{self.hash_salt}:{name_part}"
        hashed_name = hashlib.sha256(hash_input.encode("utf-8")).hexdigest()

        # Return hashed name with prefix and original extension
        return f"{self.file_prefix}{hashed_name}{ext}"

    def save(self, name, content, max_length=None):
        """
        Save file with hashed filename.

        Args:
            name: Original filename
            content: File content
            max_length: Maximum length for filename

        Returns:
            str: The hashed filename used for storage
        """
        # Generate hashed filename
        hashed_name = self._get_hashed_filename(name)

        # Call parent save with hashed name
        return super().save(hashed_name, content, max_length)

    def get_valid_filename(self, name):
        """
        Return a filename that's valid for the storage system.

        For hashed storage, we return the hashed version.
        """
        return self._get_hashed_filename(name)

    def get_available_name(self, name, max_length=None):
        """
        Return a filename that's available for the storage system.

        Since the filename is a deterministic hash of the original name, it is
        always the same for a given input.  We return it directly without
        delegating to super(), which would append a numeric/random suffix when
        the object already exists and thereby break the deterministic guarantee.
        """
        return self._get_hashed_filename(name)
