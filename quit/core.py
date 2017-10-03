import pygit2

from datetime import datetime
import logging
from os import makedirs, environ
from os.path import exists, isfile, join, expanduser
from quit.exceptions import QuitGitRepoError
from subprocess import Popen

from pygit2 import GIT_MERGE_ANALYSIS_UP_TO_DATE
from pygit2 import GIT_MERGE_ANALYSIS_FASTFORWARD
from pygit2 import GIT_MERGE_ANALYSIS_NORMAL
from pygit2 import GIT_SORT_REVERSE, GIT_RESET_HARD, GIT_STATUS_CURRENT
from pygit2 import init_repository, clone_repository
from pygit2 import Repository, Signature, RemoteCallbacks
from pygit2 import KeypairFromAgent, Keypair, UserPass
from pygit2 import credentials

from rdflib import Graph, ConjunctiveGraph, BNode, Literal
from rdflib.graph import ReadOnlyGraphAggregate
from rdflib.plugins.serializers.nquads import _nq_row as _nq

from quit.conf import Feature, QuitConfiguration
from quit.namespace import RDFS, FOAF, XSD, PROV, QUIT, is_a
from quit.graphs import RewriteGraph, InMemoryAggregatedGraph, CopyOnEditGraph
from quit.utils import graphdiff
from quit.cache import Cache, FileReference

logger = logging.getLogger('quit.core')


class Queryable:
    """A class that represents a querable graph-like object."""

    def __init__(self, **kwargs):
        pass

    def query(self, querystring):
        """Execute a SPARQL select query.

        Args:
            querystring: A string containing a SPARQL ask or select query.
        Returns:
            The SPARQL result set
        """
        pass

    def update(self, querystring, versioning=True):
        """Execute a SPARQL update query and update the store.

        This method executes a SPARQL update query and updates and commits all affected files.

        Args:
            querystring: A string containing a SPARQL upate query.
        """
        pass


class Store(Queryable):
    """A class that combines and syncronieses n-quad files and an in-memory quad store.

    This class contains information about all graphs, their corresponding URIs and
    pathes in the file system. For every Graph (context of Quad-Store) exists a
    FileReference object (n-quad) that enables versioning (with git) and persistence.
    """

    def __init__(self, store):
        """Initialize a new Store instance."""
        logger = logging.getLogger('quit.core.Store')
        logger.debug('Create an instance of Store')
        self.store = store

        return


class MemoryStore(Store):
    def __init__(self, additional_bindings=list()):
        store = ConjunctiveGraph(identifier='default')
        nsBindings = [('quit', QUIT), ('foaf', FOAF), ('prov', PROV)]

        for prefix, namespace in nsBindings + additional_bindings:
            store.bind(prefix, namespace)

        super().__init__(store=store)


class VirtualGraph(Queryable):
    def __init__(self, store):
        if not isinstance(store, InMemoryAggregatedGraph):
            raise Exception()
        self.store = store

    def query(self, querystring):
        return self.store.query(querystring)

    def update(self, querystring, versioning=True):
        return self.store.update(querystring)


class Quit(object):
    def __init__(self, config, repository, store):
        self.config = config
        self.repository = repository
        self.store = store
        self._commits = Cache()
        self._blobs = Cache()

    def _exists(self, cid):
        uri = QUIT['commit-' + cid]
        for _ in self.store.store.quads((uri, None, None, QUIT.default)):
            return True
        return False

    def rebuild(self):
        for context in self.store.contexts():
            self.store.remove((None, None, None), context)
        self.syncAll()

    def syncAll(self):
        """Synchronize store with repository data."""
        def traverse(commit, seen):
            commits = []
            merges = []

            while True:
                id = commit.id
                if id in seen:
                    break
                seen.add(id)
                if self._exists(id):
                    break
                commits.append(commit)
                parents = commit.parents
                if not parents:
                    break
                commit = parents[0]
                if len(parents) > 1:
                    merges.append((len(commits), parents[1:]))
            for idx, parents in reversed(merges):
                for parent in parents:
                    commits[idx:idx] = traverse(parent, seen)
            return commits

        seen = set()

        for name in self.repository.tags_or_branches:
            initial_commit = self.repository.revision(name)
            commits = traverse(initial_commit, seen)

            while commits:
                commit = commits.pop()
                self.syncSingle(commit)

    def syncSingle(self, commit, delta=None):
        if not self._exists(commit.id):
            self.changeset(commit, delta)

    def instance(self, commit_id=None, force=False):
        """Create and return dataset for a given commit id.

        Args:
            id: commit id of the commit to retrieve
            force: force to get the dataset from the git repository instead of the internal cache
        Returns:
            Instance of VirtualGraph representing the respective dataset
        """

        default_graphs = list()

        if commit_id:

            blobs = self._commits.get(commit_id)
            if not blobs:
                blobs = set()
                map = self.config.getgraphurifilemap()
                commit = self.repository.revision(commit_id)

                for entity in commit.node().entries(recursive=True):
                    # todo check if file was changed
                    if entity.is_file:
                        if entity.name not in map.values():
                            continue
                        graphUris = self.config.getgraphuriforfile(entity.name)
                        fixed = set((Graph(identifier=i) for i in graphUris))

                        oid = entity.oid
                        blobs.add(oid)

                        f, contexts = self._blobs.get(oid) or (None, [])
                        if not contexts:
                            tmp = ConjunctiveGraph()
                            tmp.parse(data=entity.content, format='nquads')

                            # Info: currently filter graphs from file that were not defined in
                            #       config
                            # Todo: is this the wanted behaviour?
                            contexts = set((context for context in tmp.contexts(None)
                                            if context.identifier in map)) | fixed

                            self._blobs.set(
                                oid, (FileReference(entity.name, entity.content), contexts)
                            )
                self._commits.set(commit_id, blobs)

            # now all blobs in commit are known
            for oid in blobs:
                f, contexts = self._blobs.get(oid)
                for context in contexts:
                    internal_identifier = context.identifier + '-' + str(oid)

                    if force or not self.config.hasFeature(Feature.Persistence):
                        g = context
                    else:
                        g = RewriteGraph(
                            self.store.store.store,
                            internal_identifier,
                            context.identifier
                        )
                    default_graphs.append(g)

        instance = InMemoryAggregatedGraph(
            graphs=default_graphs, identifier='default')

        return VirtualGraph(instance)

    def changeset(self, commit, delta=None):
        if (
            not self.config.hasFeature(Feature.Persistence)
        ) and (
            not self.config.hasFeature(Feature.Provenance)
        ):
            return

        g = self.store.store

        if self.config.hasFeature(Feature.Provenance):
            role_author_uri = QUIT['Author']
            role_committer_uri = QUIT['Committer']

            g.add((role_author_uri, is_a, PROV['Role']))
            g.add((role_committer_uri, is_a, PROV['Role']))

        # Create the commit
        i1 = self.instance(commit.id, True)

        commit_uri = QUIT['commit-' + commit.id]

        if self.config.hasFeature(Feature.Provenance):
            g.add((commit_uri, is_a, PROV['Activity']))

            if 'Source' in commit.properties.keys():
                g.add((commit_uri, is_a, QUIT['Import']))
                g.add((commit_uri, QUIT['dataSource'], Literal(
                    commit.properties['Source'].strip())))
            if 'Query' in commit.properties.keys():
                g.add((commit_uri, is_a, QUIT['Transformation']))
                g.add((commit_uri, QUIT['query'], Literal(
                    commit.properties['Query'].strip())))

            g.add((commit_uri, QUIT['hex'], Literal(commit.id)))
            g.add((commit_uri, PROV['startedAtTime'], Literal(
                commit.author_date, datatype=XSD.dateTime)))
            g.add((commit_uri, PROV['endedAtTime'], Literal(
                commit.committer_date, datatype=XSD.dateTime)))
            g.add((commit_uri, RDFS['comment'],
                   Literal(commit.message.strip())))

            # Author
            hash = pygit2.hash(commit.author.email).hex
            author_uri = QUIT['user-' + hash]
            g.add((commit_uri, PROV['wasAssociatedWith'], author_uri))

            g.add((author_uri, is_a, PROV['Agent']))
            g.add((author_uri, RDFS.label, Literal(commit.author.name)))
            g.add((author_uri, FOAF.mbox, Literal(commit.author.email)))

            q_author_uri = BNode()
            g.add((commit_uri, PROV['qualifiedAssociation'], q_author_uri))
            g.add((q_author_uri, is_a, PROV['Association']))
            g.add((q_author_uri, PROV['agent'], author_uri))
            g.add((q_author_uri, PROV['role'], role_author_uri))

            if commit.author.name != commit.committer.name:
                # Committer
                hash = pygit2.hash(commit.committer.email).hex
                committer_uri = QUIT['user-' + hash]
                g.add((commit_uri, PROV['wasAssociatedWith'], committer_uri))

                g.add((committer_uri, is_a, PROV['Agent']))
                g.add((committer_uri, RDFS.label, Literal(commit.committer.name)))
                g.add((committer_uri, FOAF.mbox, Literal(commit.committer.email)))

                q_committer_uri = BNode()
                g.add(
                    (commit_uri, PROV['qualifiedAssociation'], q_committer_uri))
                g.add((q_committer_uri, is_a, PROV['Association']))
                g.add((q_committer_uri, PROV['agent'], author_uri))
                g.add((q_committer_uri, PROV['role'], role_committer_uri))
            else:
                g.add((q_author_uri, PROV['role'], role_committer_uri))

            # Parents
            for parent in iter(commit.parents or []):
                parent_uri = QUIT['commit-' + parent.id]
                g.add((commit_uri, QUIT["preceedingCommit"], parent_uri))

            # Diff
            if not delta:
                parent = next(iter(commit.parents or []), None)

                i2 = self.instance(parent.id, True) if parent else None

                delta = graphdiff(i2.store if i2 else None, i1.store)

            for index, (iri, changesets) in enumerate(delta.items()):
                update_uri = QUIT['update-{}-{}'.format(commit.id, index)]
                g.add((update_uri, QUIT['graph'], iri))
                g.add((commit_uri, QUIT['updates'], update_uri))
                for (op, triples) in changesets:
                    op_uri = QUIT[op + '-' + commit.id]
                    g.add((update_uri, QUIT[op], op_uri))
                    g.addN((s, p, o, op_uri) for s, p, o in triples)

        # Entities
        map = self.config.getgraphurifilemap()

        for entity in commit.node().entries(recursive=True):
            # todo check if file was changed
            if entity.is_file:

                if entity.name not in map.values():
                    continue

                graphUris = self.config.getgraphuriforfile(entity.name)
                fixed = set((Graph(identifier=i) for i in graphUris))

                f, contexts = self._blobs.get(entity.oid) or (None, None)
                if not contexts:
                    tmp = ConjunctiveGraph()
                    tmp.parse(data=entity.content, format='nquads')

                    # Info: currently filter graphs from file that were not defined in config
                    # Todo: is this the wanted behaviour?
                    contexts = set(
                        (context for context in tmp.contexts(None) if context.identifier in map)
                    ) | fixed

                    self._blobs.set(
                        entity.oid, (FileReference(entity.name, entity.content), contexts)
                    )

                for index, context in enumerate(contexts):
                    private_uri = QUIT["graph-{}-{}".format(entity.oid, index)]

                    if (
                        self.config.hasFeature(Feature.Provenance) or
                        self.config.hasFeature(Feature.Persistence)
                    ):
                        g.add((private_uri, is_a, PROV['Entity']))
                        g.add(
                            (private_uri, PROV['specializationOf'], context.identifier))
                        g.add(
                            (private_uri, PROV['wasGeneratedBy'], commit_uri))
                    if self.config.hasFeature(Feature.Persistence):
                        g.addN((s, p, o, private_uri) for s, p, o
                               in context.triples((None, None, None)))

    def commit(self, graph, delta, message, commit_id, ref, **kwargs):
        def build_message(message, kwargs):
            out = list()
            for k, v in kwargs.items():
                if '\n' not in v:
                    out.append('%s: %s' % (k, v))
                else:
                    out.append('%s: "%s"' % (k, v))
            if message:
                out.append('')
                out.append(message)
            return "\n".join(out)

        if not delta:
            return

        index = self.repository.index(commit_id)

        blobs_new = set()
        blobs = self._commits.remove(commit_id) or []
        for oid in blobs:
            f, contexts = self._blobs.get(oid) or (None, [])
            for context in contexts:
                changesets = delta.get(context.identifier, [])
                if changesets:
                    for (op, triples) in changesets:
                        for triple in triples:
                            line = _nq(triple, context.identifier)
                            if op == 'additions':
                                f.add(line)
                            elif op == 'removals':
                                f.remove(line)
                    index.add(f.path, f.content)

                    self._blobs.remove(oid)
                    oid = index.stash[f.path][0]
                    self._blobs.set(oid, (f, contexts))
            blobs_new.add(oid)

        message = build_message(message, kwargs)
        author = self.repository._repository.default_signature

        oid = index.commit(message, author.name, author.email, ref=ref)

        if oid:
            self._commits.set(oid.hex, blobs_new)
            commit = self.repository.revision(oid.hex)
            if not self.repository.is_bare:
                self.repository._repository.checkout(
                    ref, strategy=pygit2.GIT_CHECKOUT_FORCE)
            self.syncSingle(commit, delta)
