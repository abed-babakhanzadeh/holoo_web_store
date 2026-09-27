from django.urls import path

from . import views

app_name = 'returns'
urlpatterns = [
    path('policy/', views.ReturnProcedureView.as_view(), name='procedure'),
    path('<int:order_id>/start/', views.ReturnWizardStepOneView.as_view(), name='wizard_step1'),
    path('<int:order_id>/reason/', views.ReturnWizardStepTwoView.as_view(), name='wizard_step2'),
    path('<int:order_id>/refund/', views.ReturnWizardStepThreeView.as_view(), name='wizard_step3'),
    path('success/<int:pk>/', views.ReturnSuccessView.as_view(), name='wizard_success'),
]
