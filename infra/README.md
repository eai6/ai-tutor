  Next Steps to Deploy

  1. cd infra && python -m venv venv && source venv/bin/activate && pip
  install -r requirements.txt
  2. pulumi stack init dev and set secrets via pulumi config set --secret
  ...
  3. pulumi up to provision Azure resources
  4. Build & push: docker build -t <acr>/aitutor:latest . && docker push ...
  5. Set GitHub secrets (AZURE_CREDENTIALS, ACR_LOGIN_SERVER,
  AZURE_RESOURCE_GROUP, CONTAINER_APP_NAME) for CI/CD
  6. Run migrations and data migration as described in the plan



  az account set --subscription "Microsoft Azure Sponsorship"