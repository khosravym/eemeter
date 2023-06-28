FROM python:3.11.2

# sphinxcontrib-spelling dependency
RUN apt-get update \
  && apt-get install -yqq libenchant-2-dev

# Use a base Python image

# Set the working directory in the container
WORKDIR /app

# Copy the pyproject.toml and poetry.lock files to the container
COPY pyproject.toml poetry.lock ./

# Install Poetry
RUN pip install poetry

# Copy the rest of the project files to the container
COPY . /app

# Install project dependencies
RUN POETRY_VIRTUALENVS_CREATE=false poetry install

