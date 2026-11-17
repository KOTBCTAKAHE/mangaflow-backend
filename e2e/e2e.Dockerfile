FROM mangaflow-backend:latest
USER root
COPY e2e/fixture_source /srv/e2e_source
RUN pip install /srv/e2e_source
USER mangaflow
