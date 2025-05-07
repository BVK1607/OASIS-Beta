from django.core.management.base import BaseCommand
from portfolio.models import Portfolio
from portfolio.services import PortfolioNAVService
import logging

logger = logging.getLogger(__name__)

class Command(BaseCommand):
    help = 'Backfill NAV and holding returns for all portfolios using forward-fill and completeness flags.'

    def handle(self, *args, **options):
        portfolios = Portfolio.objects.all()
        for portfolio in portfolios:
            logger.info(f"Backfilling NAV timeseries for portfolio {portfolio.id} ({portfolio.name})")
            result = PortfolioNAVService.backfill_portfolio_time_series(portfolio)
            if result:
                logger.info(f"Successfully backfilled NAV for portfolio {portfolio.id}")
            else:
                logger.warning(f"No holdings or failed for portfolio {portfolio.id}")
        self.stdout.write(self.style.SUCCESS('NAV timeseries backfill complete.')) 