# Generated by Django 3.1.6 on 2021-02-25 21:52

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0031_auto_20210211_0141'),
    ]

    operations = [
        migrations.AlterField(
            model_name='tournament',
            name='qual_type',
            field=models.IntegerField(choices=[(0, 'Regular (Points)'), (1, 'Brandeis IV'), (2, 'Yale IV'), (3, 'NorthAms'), (4, 'Expansion'), (5, 'Worlds'), (6, 'NAUDC'), (7, 'ProAms'), (8, 'Nationals'), (9, 'Novice'), (10, 'Gender Minority'), (11, 'Online No Points'), (12, 'Online Points'), (13, 'Online Proams Points')], default=0, verbose_name='Tournament Type'),
        ),
    ]
