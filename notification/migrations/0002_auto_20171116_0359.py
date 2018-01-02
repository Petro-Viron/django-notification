# -*- coding: utf-8 -*-
# Generated by Django 1.10.6 on 2017-11-16 03:59
from __future__ import unicode_literals

from django.db import migrations, models
import django.utils.timezone


class Migration(migrations.Migration):

    dependencies = [
        ('notification', '0001_initial'),
    ]

    operations = [
        migrations.AlterField(
            model_name='notice',
            name='added',
            field=models.DateTimeField(db_index=True, default=django.utils.timezone.now, verbose_name='added'),
        ),
    ]