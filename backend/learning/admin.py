from django.contrib import admin
from .models import LearningContent

@admin.register(LearningContent)
class LearningContentAdmin(admin.ModelAdmin):
    list_display = ('title', 'created_at', 'updated_at')
    search_fields = ('title', 'content')
    ordering = ('-created_at',)
