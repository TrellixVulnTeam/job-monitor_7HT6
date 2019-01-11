#!/usr/bin/env node

const path = require('path');
const fs = require('fs');
const { ApolloServer, gql } = require('apollo-server');
const { MongoClient, ObjectID } = require('mongodb');
const { GraphQLScalarType } = require('graphql');
const { Kind } = require('graphql/language');
const process = require('process');
const { InfluxDB } = require('influx');

const host = process.env.JOBMONITOR_METADATA_HOST;
const port = process.env.JOBMONITOR_METADATA_PORT;
const database = process.env.JOBMONITOR_METADATA_DB;

const HEARTBEAT_INTERVAL = 10; // seconds

const influx = new InfluxDB({
    host: process.env.JOBMONITOR_TIMESERIES_HOST,
    port: process.env.JOBMONITOR_TIMESERIES_PORT,
    database: process.env.JOBMONITOR_TIMESERIES_DB,
});

let mongo;

// The GraphQL schema
const typeDefs = gql`
    scalar Date
    scalar Object
    scalar Dictionary

    type Job {
        id: ID!
        user: String!
        project: String!
        experiment: String!
        job: String!
        status: Status!
        host: String!
        outputDirectory: String
        creationTime: Date!
        startTime: Date
        endTime: Date
        logs: String
        config: [Config]
        annotations: [Annotation]
        "If an error occurred (status=FAILED), this is a string representation of you the exception"
        exception: String
        environment: Environment!
        "Get timeseries that satisfy zero or more requirements (strings accept regex)"
        timeseries(measurement: String, tags: String): [Timeseries]
        "How close to done are we?"
        progress: Float
        textFile(filename: String!): String
        jsonFile(filename: String!): Dictionary
    }

    enum Status {
        CREATED
        SCHEDULED
        RUNNING
        FINISHED
        CANCELED
        FAILED
        UNRESPONSIVE
    }
    type Config {
        key: String
        value: Object
    }
    type Annotation {
        key: String
        value: String
    }
    type CloneConfiguration {
        path: String
    }
    type Environment {
        script: String
        clone: CloneConfiguration
    }
    type ValueList {
        key: String
        value: Float
    }
    type Timeseries {
        measurement: String!
        tags: Dictionary!
        values: [Dictionary]
        currentValue: Dictionary!
        maxValue: Dictionary!
        minValue: Dictionary!
        jobId: ID!
    }
    type Query {
        "Get a job entry by ID"
        job(id: ID!): Job
        "Get a list of jobs satisfying the specified criteria. 'job' allows for regex"
        jobs(ids: [ID], user: String, project: String, experiment: String, job: String, status: Status, limit: Int): [Job]
    }
`;

function parseJobFromDatabase(entry) {
    return {
        id: entry._id.toString(),
        user: entry.user,
        project: entry.project,
        experiment: entry.experiment,
        job: entry.job,
        status: jobStatus(entry),
        host: entry.host,
        outputDirectory: entry.output_dir,
        creationTime: entry.creation_time,
        startTime: entry.start_time,
        endTime: entry.end_time,
        config: Object.entries(entry.config || {}).map(([k, v]) => ({ key: k, value: v})),
        exception: entry.exception,
        annotations: Object.entries(entry.annotations || {}).map(([k, v]) => ({ key: k, value: v})),
        environment: entry.environment || entry.initialization,
        progress: (entry.state || {}).progress,
    }
}

// A map of functions which return data for the schema.
const resolvers = {
    Query: {
        job: (root, args, context, info) => {
            return mongo
                .collection('job')
                .findOne({ _id: ObjectID(args.id) })
                .then(parseJobFromDatabase);
        },
        jobs: (root, args, context, info) => {
            const limit = args.limit || 0;
            delete args.limit;
            const ids = args.ids;
            delete args.ids;
            return mongo
                .collection('job')
                .find({ ...args, ...statusQuery(args.status), ...jobRegexQuery(args.job), ...idsQuery(ids) })
                .sort({'creation_time': -1})
                .limit(limit)
                .toArray()
                .then(entries => entries.map(parseJobFromDatabase));
        },
    },
    Job: {
        logs: (job, args, context, info) => {
            if (job.outputDirectory == null) return null;
            const logFile = path.join(process.env.JOBMONITOR_RESULTS_DIR, job.outputDirectory, 'output.txt');
            if (!fs.existsSync(logFile)) return null;
            return new Promise((resolve, reject) => fs.readFile(logFile, 'utf8', (err, value) => {
                if (err) reject(err);
                resolve(value);
            }));
        },
        textFile: (job, args, context, info) => {
            const filename = args['filename'];
            const filepath = path.join(process.env.JOBMONITOR_RESULTS_DIR, job.outputDirectory, filename);
            if (!fs.existsSync(filepath)) return null;
            return new Promise((resolve, reject) => fs.readFile(filepath, 'utf8', (err, value) => {
                if (err) reject(err);
                resolve(value);
            }));
        },
        jsonFile: (job, args, context, info) => {
            const filename = args['filename'];
            const filepath = path.join(process.env.JOBMONITOR_RESULTS_DIR, job.outputDirectory, filename);
            if (!fs.existsSync(filepath)) return null;
            return new Promise((resolve, reject) => fs.readFile(filepath, 'utf8', (err, value) => {
                if (err) reject(err);
                resolve(value);
            })).then(JSON.parse);
        },
        timeseries: (job, args, context, info) => {
            const fromQuery = (args.measurement != null) ? `FROM /${args.measurement}/` : '';
            let conditions = (args.tags || "")
                .split(',')
                .filter(x => x != "")
                .map(condition => condition.split('='))
                .map(([key, value]) => `AND ${key} = '${value}'`)
                .join(' ');
            return influx
                .query(`SHOW SERIES ${fromQuery} WHERE job_id='${job.id}' ${conditions}`)
                .then((res) => {
                    if (res.groups().length == 0) {
                        return [];
                    } else {
                        return res.groups()[0].rows.map(x => parseSeries(x.key, job.id));
                    }
                })
        }
    },
    Timeseries: {
        values: (timeseries, args, context, info) => {
            const { measurement, jobId, tags } = timeseries;
            const whereClause = Object.entries(tags).map(([key, value]) => ` and ${key}='${value}'`).join(' ');
            const query = `SELECT *::field FROM ${measurement} WHERE job_id='${jobId}'${whereClause} GROUP BY *`;
            return influx
                .query(query)
                .then((res) => {
                    const { tags, rows } = res.groups()[0]
                    const tagNames = new Set(Object.keys(tags));
                    return rows.map(row => {
                        let fields = {}
                        Object.entries(row).forEach(([key, value]) => {
                            if (!tagNames.has(key)) {
                                if (key === 'time') {
                                    fields[key] = Math.floor(value.getNanoTime() / 1000000); // convert to milliseconds
                                } else {
                                    fields[key] = value;
                                }
                            }
                        });
                        return fields;
                    });
                })
        },
        currentValue: getValueFromTimeseries('LAST', 'last'),
        maxValue: getValueFromTimeseries('MAX', 'max'),
        minValue: getValueFromTimeseries('MIN', 'min'),
    },
    Date: new GraphQLScalarType({
        name: 'Date',
        description: 'Milliseconds since 1970',
        parseValue(value) {
            return new Date(value);
        },
        serialize(value) {
            return value.getTime();
        },
        parseLiteral(ast) {
            if (ast.kind === Kind.INT) {
                return new Date(ast.value)
            } else {
                return null;
            }
        },
    }),
    Object: new GraphQLScalarType({
        name: 'Object',
        description: 'Arbitrary object',
        parseValue: (value) => {
            try {
                return JSON.parse(value);
            } catch (error) { // Then it's probably a string
                return value;
            }
        },
        serialize: (value) => {
            if (typeof value === 'object') {
                return JSON.stringify(value);
            } else {
                return value;
            }
        },
        parseLiteral: (ast) => {
            if (ast.kind === Kind.STRING) {
                try {
                    return JSON.parse(ast.value);
                } catch (err) {
                    return ast.value;
                }
            } else {
                return ast.value;
            }
        }
    }),
    Dictionary: new GraphQLScalarType({
        name: 'Dictionary',
        description: 'Object with string keys and numeric values',
        parseValue: (value) => {
            return value;
        },
        serialize: (value) => {
            return value;
        },
        parseLiteral: (ast) => {
            if (ast.kind === Kind.OBJECT) {
                return ast.value;
            } else {
                return null;
            }
        }
    })
};

function statusQuery(status) {
    let statusSearch = { };
    const heartbeatThreshold = new Date(Date.now() - 2 * HEARTBEAT_INTERVAL * 1000);
    if (status === 'UNRESPONSIVE') {
        statusSearch['status'] = 'RUNNING';
        statusSearch['last_heartbeat_time'] = {'$lte': heartbeatThreshold };
    } else if (status === 'RUNNING') {
        statusSearch['status'] = 'RUNNING';
        statusSearch['last_heartbeat_time'] = {'$gt': heartbeatThreshold };
    } else if (status) {
        statusSearch['status'] = status;
    }
    return statusSearch;
}

function idsQuery(ids) {
    if (ids == null) {
        return {};
    } else {
        return { _id: { '$in': ids.map(id => ObjectID(id)) } };
    }
}

function getValueFromTimeseries(operator, name_prefix) {
    return (timeseries, args, context, info) => {
        const { measurement, jobId, tags } = timeseries;
        const whereClause = Object.entries(tags).map(([key, value]) => ` and ${key}='${value}'`).join(' ');
        const query = `SELECT ${operator}(*::field) FROM ${measurement} WHERE job_id='${jobId}'${whereClause} GROUP BY *`;
        return influx
            .query(query)
            .then((res) => {
                const { _, rows } = res.groups()[0]
                return rows.map(row => {
                    let fields = {}
                    Object.entries(row).forEach(([key, value]) => {
                        if (key.indexOf(name_prefix + '') === 0) {
                            fields[key.substr(name_prefix.length + 1)] = value;
                        }
                    });
                    return fields;
                });
            })
    };
}

function jobRegexQuery(jobRegex) {
    if (jobRegex != null) {
        return { 'name': null, 'job': { '$regex': '^' + jobRegex + '$' } }
    } else {
        return {};
    }
}

function jobStatus(entry) {
    // Job status from a database entry
    let status = entry.status;
    if (status === 'RUNNING') {
        // Check if there has been a heartbeat recently
        const timeSinceLastHeartbeat = Date.now() - entry.last_heartbeat_time;
        const probablyDead = timeSinceLastHeartbeat > 2 * HEARTBEAT_INTERVAL * 1000;
        if (probablyDead) {
            status = 'UNRESPONSIVE';
        }
    }
    return status
}

function parseSeries(seriesString, jobId) {
    const tagBlacklist = ['experiment', 'host', 'influxdb_database', 'job', 'job_id', 'project', 'user'];
    [measurement, ...tagStrings] = seriesString.split(',');
    const tagList = tagStrings
        .map(s => {
            const [key, value] = s.split('=');
            return { key, value };
        }).filter(({ key }) => tagBlacklist.indexOf(key) === -1)
    let tags = {}
    for (let { key, value } of tagList) {
        tags[key] = value;
    }
    return { measurement, tags, jobId };
}

const server = new ApolloServer({
    typeDefs,
    resolvers,
    cors: { origin: true },
});

MongoClient
    .connect(`mongodb://${host}:${port}/${database}`, { useNewUrlParser: true })
    .then((db) => mongo = db.db())
    .then(() => server.listen())
    .then(({ url }) => {
        console.log(`🚀 Server ready at ${url}`)
    });
