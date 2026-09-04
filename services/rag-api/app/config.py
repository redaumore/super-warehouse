#!/usr/bin/env python3
"""
app/config.py
=============
Configuración centralizada y tipada de la aplicación utilizando Pydantic BaseSettings.
"""

import os
from pathlib import Path
from typing import Optional
from dotenv import load_dotenv
from pydantic_settings import BaseSettings
from pydantic import Field

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    """Configuración global de la aplicación y conexiones."""
    
    # Metadata
    APP_NAME: str = "RAG Multi-Proveedor Catalog API"
    APP_VERSION: str = "1.0.0"
    DEBUG: bool = Field(default=False)
    
    # Directorios base
    BASE_PATH: Path = BASE_DIR
    DATA_DIR: Path = BASE_DIR / "data"
    ARTIFACTS_DIR: Path = BASE_DIR / "data" / "artifacts"
    UPLOADS_DIR: Path = BASE_DIR / "data" / "uploads"
    RAW_PDFS_DIR: Path = BASE_DIR / "data" / "raw_pdfs"
    
    # API Keys & LLM Providers
    OPENAI_API_KEY: Optional[str] = Field(default_factory=lambda: os.getenv("OPENAI_API_KEY"))
    GOOGLE_API_KEY: Optional[str] = Field(default_factory=lambda: os.getenv("GOOGLE_API_KEY") or os.getenv("GEMINI_API_KEY"))
    
    # PostgreSQL & pgvector
    DATABASE_URL: Optional[str] = Field(default_factory=lambda: os.getenv("DATABASE_URL"))
    POSTGRES_USER: str = Field(default_factory=lambda: os.getenv("POSTGRES_USER", "postgres"))
    POSTGRES_PASSWORD: str = Field(default_factory=lambda: os.getenv("POSTGRES_PASSWORD", "postgres"))
    POSTGRES_HOST: str = Field(default_factory=lambda: os.getenv("POSTGRES_HOST", "localhost"))
    POSTGRES_PORT: str = Field(default_factory=lambda: os.getenv("POSTGRES_PORT", "5432"))
    POSTGRES_DB: str = Field(default_factory=lambda: os.getenv("POSTGRES_DB", "postgres"))
    
    # Modelos y parámetros por defecto
    DEFAULT_TABLE_NAME: str = "catalogo_productos_rag"
    DEFAULT_LUNA_MODEL: str = "gpt-5.6-luna"
    DEFAULT_EMBEDDING_MODEL: str = "text-embedding-3-large"
    DEFAULT_EMBEDDING_DIM: int = 256
    DEFAULT_LLM_MODEL: str = "gpt-4o"
    DEFAULT_RERANKER_MODEL: str = "BAAI/bge-reranker-v2-m3"
    
    def get_db_url(self) -> str:
        """Construye la URL de conexión a PostgreSQL."""
        if self.DATABASE_URL:
            return self.DATABASE_URL
        return f"postgresql://{self.POSTGRES_USER}:{self.POSTGRES_PASSWORD}@{self.POSTGRES_HOST}:{self.POSTGRES_PORT}/{self.POSTGRES_DB}"


settings = Settings()

# Asegurar existencia de directorios de almacenamiento
os.makedirs(settings.ARTIFACTS_DIR, exist_ok=True)
os.makedirs(settings.UPLOADS_DIR, exist_ok=True)
os.makedirs(settings.RAW_PDFS_DIR, exist_ok=True)
