from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('portfolio', '0002_recreate_marketprice'),
    ]

    operations = [
        migrations.RemoveField(
            model_name='marketprice',
            name='volume',
        ),
    ] 