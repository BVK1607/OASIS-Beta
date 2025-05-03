from django.core.management.base import BaseCommand
from django.db import connection
import logging

logger = logging.getLogger(__name__)

class Command(BaseCommand):
    help = 'Check the current state of the database tables and indexes'

    def handle(self, *args, **options):
        self.stdout.write("Checking database state...")
        
        # Check table existence
        self.stdout.write("\nChecking table existence:")
        with connection.cursor() as cursor:
            cursor.execute("""
                SELECT name FROM sqlite_master 
                WHERE type='table'
                ORDER BY name
            """)
            tables = cursor.fetchall()
            
            expected_tables = [
                'portfolio',
                'holding',
                'holding_returns',
                'portfolio_nav',
                'portfolio_marketprice',
                'benchmark_return',
                'rebalance_plan',
                'trade_history'
            ]
            
            for table in tables:
                table_name = table[0]
                self.stdout.write(f"Found table: {table_name}")
                if table_name in expected_tables:
                    expected_tables.remove(table_name)
            
            if expected_tables:
                self.stdout.write(self.style.ERROR("\nMissing tables:"))
                for table in expected_tables:
                    self.stdout.write(self.style.ERROR(f"- {table}"))

        # Check indexes
        self.stdout.write("\nChecking indexes:")
        with connection.cursor() as cursor:
            cursor.execute("""
                SELECT name, tbl_name, sql 
                FROM sqlite_master 
                WHERE type='index'
                ORDER BY tbl_name, name
            """)
            indexes = cursor.fetchall()
            
            for index in indexes:
                self.stdout.write(f"Found index: {index[0]} on table {index[1]}")
                self.stdout.write(f"SQL: {index[2]}\n")

        # Check table structures
        self.stdout.write("\nChecking table structures:")
        with connection.cursor() as cursor:
            for table in tables:
                table_name = table[0]
                cursor.execute(f"PRAGMA table_info({table_name})")
                columns = cursor.fetchall()
                self.stdout.write(f"\nTable: {table_name}")
                for col in columns:
                    self.stdout.write(f"  Column: {col[1]} ({col[2]})")

        # Check for data inconsistencies
        self.stdout.write("\nChecking for data inconsistencies:")
        with connection.cursor() as cursor:
            # Check for NULL values in critical fields
            cursor.execute("""
                SELECT COUNT(*) 
                FROM portfolio_nav 
                WHERE total_value IS NULL OR nav IS NULL
            """)
            null_navs = cursor.fetchone()[0]
            if null_navs > 0:
                self.stdout.write(self.style.ERROR(f"Found {null_navs} NULL values in portfolio_nav"))

            cursor.execute("""
                SELECT COUNT(*) 
                FROM holding_returns 
                WHERE daily_return IS NULL
            """)
            null_returns = cursor.fetchone()[0]
            if null_returns > 0:
                self.stdout.write(self.style.ERROR(f"Found {null_returns} NULL values in holding_returns"))

        self.stdout.write(self.style.SUCCESS("\nDatabase state check completed")) 