from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('portfolio', '0001_initial'),
    ]

    operations = [
        migrations.RunSQL(
            'DROP TABLE IF EXISTS portfolio_marketprice;',
            state_operations=[
                migrations.DeleteModel(
                    name='MarketPrice',
                ),
            ],
        ),
        migrations.CreateModel(
            name='MarketPrice',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('ticker', models.CharField(max_length=20)),
                ('date', models.DateField()),
                ('price', models.DecimalField(decimal_places=4, max_digits=20)),
                ('volume', models.BigIntegerField(null=True)),
                ('last_updated', models.DateTimeField(auto_now=True)),
                ('portfolio', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='market_prices', to='portfolio.portfolio')),
            ],
            options={
                'db_table': 'portfolio_marketprice',
                'unique_together': {('portfolio', 'ticker', 'date')},
                'indexes': [
                    models.Index(fields=['ticker'], name='mp_ticker_idx'),
                    models.Index(fields=['date'], name='mp_date_idx'),
                    models.Index(fields=['portfolio', 'ticker'], name='mp_port_ticker_idx'),
                    models.Index(fields=['portfolio', 'ticker', 'date'], name='mp_port_ticker_date_idx'),
                ],
            },
        ),
    ] 