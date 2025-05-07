from django.core.management.base import BaseCommand
from django.db import connection
from django.db import transaction
import logging

logger = logging.getLogger(__name__)

class Command(BaseCommand):
    help = 'Fix database table names and migrate data to correct tables'

    def handle(self, *args, **options):
        self.stdout.write("Starting database table fixes...")
        
        with transaction.atomic():
            try:
                # Step 1: Create new tables with correct names if they don't exist
                with connection.cursor() as cursor:
                    # Create holding_returns table
                    cursor.execute("""
                        CREATE TABLE IF NOT EXISTS holding_returns (
                            id INTEGER PRIMARY KEY AUTOINCREMENT,
                            portfolio_id INTEGER NOT NULL,
                            holding_id INTEGER NOT NULL,
                            date DATE NOT NULL,
                            daily_return DECIMAL(10,6),
                            FOREIGN KEY (portfolio_id) REFERENCES portfolio_portfolio (id),
                            FOREIGN KEY (holding_id) REFERENCES portfolio_holding (id)
                        )
                    """)
                    
                    # Create portfolio_nav table
                    cursor.execute("""
                        CREATE TABLE IF NOT EXISTS portfolio_nav (
                            id INTEGER PRIMARY KEY AUTOINCREMENT,
                            portfolio_id INTEGER NOT NULL,
                            date DATE NOT NULL,
                            total_value DECIMAL(20,6),
                            nav DECIMAL(20,6),
                            FOREIGN KEY (portfolio_id) REFERENCES portfolio_portfolio (id)
                        )
                    """)
                    
                    # Step 2: Migrate data from old tables to new ones
                    # Migrate holding returns data
                    cursor.execute("""
                        INSERT INTO holding_returns (portfolio_id, holding_id, date, daily_return)
                        SELECT portfolio_id, holding_id, date, daily_return
                        FROM portfolio_holding_return
                        WHERE NOT EXISTS (
                            SELECT 1 FROM holding_returns hr
                            WHERE hr.portfolio_id = portfolio_holding_return.portfolio_id
                            AND hr.holding_id = portfolio_holding_return.holding_id
                            AND hr.date = portfolio_holding_return.date
                        )
                    """)
                    
                    # Migrate portfolio nav data
                    cursor.execute("""
                        INSERT INTO portfolio_nav (portfolio_id, date, total_value, nav)
                        SELECT portfolio_id, date, total_value, nav
                        FROM portfolio_nav
                        WHERE NOT EXISTS (
                            SELECT 1 FROM portfolio_nav pn
                            WHERE pn.portfolio_id = portfolio_nav.portfolio_id
                            AND pn.date = portfolio_nav.date
                        )
                    """)
                    
                    # Step 3: Create indexes on new tables
                    cursor.execute("""
                        CREATE INDEX IF NOT EXISTS holding_returns_port_date_idx 
                        ON holding_returns (portfolio_id, date)
                    """)
                    
                    cursor.execute("""
                        CREATE INDEX IF NOT EXISTS portfolio_nav_port_date_idx 
                        ON portfolio_nav (portfolio_id, date)
                    """)
                    
                    # Step 4: Verify the migration
                    cursor.execute("SELECT COUNT(*) FROM holding_returns")
                    new_returns_count = cursor.fetchone()[0]
                    cursor.execute("SELECT COUNT(*) FROM portfolio_holding_return")
                    old_returns_count = cursor.fetchone()[0]
                    
                    cursor.execute("SELECT COUNT(*) FROM portfolio_nav")
                    new_nav_count = cursor.fetchone()[0]
                    cursor.execute("SELECT COUNT(*) FROM portfolio_nav")
                    old_nav_count = cursor.fetchone()[0]
                    
                    self.stdout.write(self.style.SUCCESS(f"""
                        Migration completed:
                        - Holding Returns: {old_returns_count} -> {new_returns_count} records
                        - Portfolio NAV: {old_nav_count} -> {new_nav_count} records
                    """))
                    
                    # Step 5: Drop old tables (commented out for safety - uncomment after verification)
                    # cursor.execute("DROP TABLE IF EXISTS portfolio_holding_return")
                    # cursor.execute("DROP TABLE IF EXISTS portfolio_nav")
                    
            except Exception as e:
                self.stdout.write(self.style.ERROR(f"Error during migration: {str(e)}"))
                raise 