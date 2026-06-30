from django.contrib import admin

from .models import CaseReview, ReviewConfig

admin.site.register(CaseReview)
admin.site.register(ReviewConfig)
