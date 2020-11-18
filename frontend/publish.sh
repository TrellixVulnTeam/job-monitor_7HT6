#!/bin/bash

VERSION=1.2

# This file contains user-specific stuff and is generated automatically from the template
docker build . -t frontend \
  --build-arg GRAPHQL_HOST=vogels-graphql.mlo.k8s.iccluster.epfl.ch \
  --build-arg GRAPHQL_PORT=30004 \
&& docker tag frontend ic-registry.epfl.ch/mlo/vogels_frontend:$VERSION \
&& docker push ic-registry.epfl.ch/mlo/vogels_frontend:$VERSION \
&& docker tag frontend ic-registry.epfl.ch/mlo/vogels_frontend:latest \
&& docker push ic-registry.epfl.ch/mlo/vogels_frontend:latest
