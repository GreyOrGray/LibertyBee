"""
Database Manager - Handle all database connections and basic operations
"""
import pyodbc
import json
import logging
import os
from contextlib import contextmanager
from typing import Optional, Dict, Any, List, Tuple
from dataclasses import dataclass

CONFIG_FILE_PATH = os.getenv("ENV_CONFIG")

@dataclass
class DatabaseConfig:
    """Database connection configuration"""
    driver: str
    server: str
    database: str
    username: str
    password: str
    trusted_connection: bool = False
    encrypt: bool = False

class DatabaseManager:
    """Centralized database connection and operations management"""

    def __init__(self, config_file: str):
        self.config = self._load_config(config_file)
        self.connection_string = self._build_connection_string()
        self.logger = logging.getLogger(__name__)
        self.event_logger = None
        self.debug_tracer = None

    def set_debug_tracer(self, tracer) -> None:
        """Wire up a DebugTracer to log every DB operation."""
        self.debug_tracer = tracer

    def _log_db(self, method: str, sql: str, params, result) -> None:
        if self.debug_tracer:
            rows = result if isinstance(result, int) else (len(result) if result is not None else None)
            self.debug_tracer.log_db_call(method, sql, params, rows)

    def _load_config(self, config_file: str) -> DatabaseConfig:
        """Load database configuration from JSON file"""
        try:
            with open(config_file, 'r') as f:
                config_data = json.load(f)
            return DatabaseConfig(**config_data)
        except FileNotFoundError:
            raise FileNotFoundError(f"Database config file '{config_file}' not found")
        except json.JSONDecodeError:
            raise ValueError(f"Invalid JSON in config file '{config_file}'")

    def _build_connection_string(self) -> str:
        """Build pyodbc connection string from config"""
        if self.config.trusted_connection:
            return (f"DRIVER={self.config.driver};"
                   f"SERVER={self.config.server};"
                   f"DATABASE={self.config.database};"
                   f"Trusted_Connection=yes;")
        else:
            return (f"DRIVER={self.config.driver};"
                   f"SERVER={self.config.server};"
                   f"DATABASE={self.config.database};"
                   f"UID={self.config.username};"
                   f"PWD={self.config.password};")

    @contextmanager
    def get_connection(self):
        """Context manager for database connections"""
        conn = None
        try:
            conn = pyodbc.connect(self.connection_string)
            # Set QUOTED_IDENTIFIER ON (required for indexed views, computed columns, etc.)
            cursor = conn.cursor()
            cursor.execute("SET QUOTED_IDENTIFIER ON")
            cursor.close()
            yield conn
        except pyodbc.Error as e:
            self.logger.error(f"Database connection failed: {e}")
            raise
        finally:
            if conn:
                conn.close()


    def execute_query(self, query: str, params: Optional[Tuple] = None) -> List[Tuple]:
        """Execute a SELECT query and return results"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            if params:
                cursor.execute(query, params)
            else:
                cursor.execute(query)
            results = cursor.fetchall()
            self._log_db('execute_query', query, params, results)
            return results

    def execute_non_query(self, query: str, params: Optional[Tuple] = None) -> int:
        """Execute INSERT/UPDATE/DELETE and return affected rows"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            if params:
                cursor.execute(query, params)
            else:
                cursor.execute(query)
            affected_rows = cursor.rowcount
            conn.commit()
            self._log_db('execute_non_query', query, params, affected_rows)
            return affected_rows

    def execute_insert_returning(self, query: str, params: Optional[Tuple] = None) -> List[Tuple]:
        """Execute INSERT with OUTPUT clause and commit, returning results"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            if params:
                cursor.execute(query, params)
            else:
                cursor.execute(query)
            results = cursor.fetchall()
            conn.commit()
            self._log_db('execute_insert_returning', query, params, results)
            return results

    def execute_scalar(self, query: str, params: Optional[Tuple] = None) -> Any:
        """Execute query and return single value"""
        results = self.execute_query(query, params)
        if results:
            return results[0][0]
        return None
    