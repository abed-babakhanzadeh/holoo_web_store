@echo off
cd /d D:\MosaviMart-Website\holoo_web_store
"C:\Users\yasaman\AppData\Local\Programs\Python\Python314\python.exe" -m celery -A config beat -l info