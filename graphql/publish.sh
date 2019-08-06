#!/bin/bash

VERSION=1.4

docker build . -t graphql \
&& docker tag graphql ic-registry.epfl.ch/mlo/vogels_graphql:$VERSION \
&& docker tag graphql tvogels/mlo-graphql:$VERSION \
&& docker push ic-registry.epfl.ch/mlo/vogels_graphql:$VERSION \
&& docker push tvogels/mlo-graphql:$VERSION
