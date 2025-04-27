from django.core.management.base import BaseCommand
from portfolio.models import Portfolio

class Command(BaseCommand):
    help = 'Creates an initial portfolio for testing'

    def handle(self, *args, **kwargs):
        # Check if portfolio with ID 1 exists
        if not Portfolio.objects.filter(id=1).exists():
            portfolio = Portfolio.objects.create(
                name='My Portfolio',
                description='Initial portfolio for testing'
            )
            self.stdout.write(self.style.SUCCESS(f'Successfully created portfolio with ID {portfolio.id}'))
        else:
            self.stdout.write(self.style.SUCCESS('Portfolio with ID 1 already exists')) 