import argparse
import Bio
import Bio.Phylo
import numpy as np
import os, json, sys
import pandas as pd
from collections import defaultdict, OrderedDict
from .__version__ import __version__

from augur.data import as_file
from augur.io.file import open_file

from augur.types import ValidationMode

from augur.util_support.color_parser import ColorParser
from augur.util_support.node_data_reader import NodeDataReader


def get_json_name(args, default=None):
    if args.output_node_data:
        return args.output_node_data
    else:
        if default:
            print("WARNING: no name for the output file was specified. Writing results to %s."%default, file=sys.stderr)
            return default
        else:
            raise ValueError("Please specify a name for the JSON file containing the results.")


class InvalidTreeError(Exception):
    """Represents an error loading a phylogenetic tree from a filename.
    """
    pass


def read_tree(fname, min_terminals=3):
    """Safely load a tree from a given filename or raise an error if the file does
    not contain a valid tree.

    Parameters
    ----------
    fname : str
        name of a file containing a phylogenetic tree

    min_terminals : int
        minimum number of terminals required for the parsed tree as a sanity
        check on the tree

    Raises
    ------
    InvalidTreeError
        If the given file exists but does not seem to contain a valid tree format.

    Returns
    -------
    Bio.Phylo.BaseTree.Tree :
        BioPython tree instance

    """
    T = None
    supported_tree_formats = ["newick", "nexus"]
    for fmt in supported_tree_formats:
        try:
            T = Bio.Phylo.read(fname, fmt)

            # Check the sanity of the parsed tree to handle cases when non-tree
            # data are still successfully parsed by BioPython. Too few terminals
            # in a tree indicates that the input is not valid.
            if T.count_terminals() < min_terminals:
                T = None
            else:
                break
        except ValueError:
            # We cannot open the tree in the current format, so we will try
            # another.
            pass

    # If the tree cannot be loaded, raise an error to that effect.
    if T is None:
        raise InvalidTreeError(
            "Could not read the given tree %s using the following supported formats: %s" % (fname, ", ".join(supported_tree_formats))
        )

    return T


def read_node_data(fnames, tree=None, validation_mode=ValidationMode.ERROR):
    return NodeDataReader(fnames, tree, validation_mode).read()


def write_json(data, file_name, indent=(None if os.environ.get("AUGUR_MINIFY_JSON") else 2), include_version=True):
    """
    Write ``data`` as JSON to the given ``file_name``, creating parent directories
    if necessary. The augur version is included as a top-level key "augur_version".

    Parameters
    ----------
    data : dict
        data to write out to JSON
    file_name : str
        file name to write to
    indent : int or None, optional
        JSON indentation level. Default is `None` if the environment variable `AUGUR_MINIFY_JSON`
        is truthy, else 1
    include_version : bool, optional
        Include the augur version. Default: `True`.

    Raises
    ------
    OSError
    """
    #in case parent folder does not exist yet
    parent_directory = os.path.dirname(file_name)
    if parent_directory and not os.path.exists(parent_directory):
        try:
            os.makedirs(parent_directory)
        except OSError: #Guard against race condition
            if not os.path.isdir(parent_directory):
                raise

    if include_version:
        data["generated_by"] = {"program": "augur", "version": get_augur_version()}
    with open(file_name, 'w', encoding='utf-8') as handle:
        sort_keys = False if isinstance(data, OrderedDict) else True
        json.dump(data, handle, indent=indent, sort_keys=sort_keys, cls=AugurJSONEncoder)


class AugurJSONEncoder(json.JSONEncoder):
    """
    A custom JSONEncoder subclass to serialize data types used for various data
    stored in dictionary format.
    """
    def default(self, obj):
        if isinstance(obj, np.integer):
            return int(obj)
        if isinstance(obj, np.floating):
            return float(obj)
        if isinstance(obj, np.ndarray):
            return obj.tolist()
        if isinstance(obj, pd.Series):
            return obj.tolist()
        return super().default(obj)


def load_features(reference, feature_names=None):
    #read in appropriately whether GFF or Genbank
    #checks explicitly for GFF otherwise assumes Genbank
    if not os.path.isfile(reference):
        print("ERROR: reference sequence not found. looking for", reference)
        return None

    features = {}
    if '.gff' in reference.lower():
        #looks for 'gene' and 'gene' as best for TB
        from BCBio import GFF
        limit_info = dict( gff_type = ['gene', 'source'] )

        with open(reference, encoding='utf-8') as in_handle:
            for rec in GFF.parse(in_handle, limit_info=limit_info):
                for feat in rec.features:
                    # Check for gene names stored in qualifiers commonly used by
                    # virus-specific gene maps first (e.g., 'gene',
                    # 'gene_name'). Then, check for qualifiers used by non-viral
                    # pathogens (e.g., 'locus_tag').
                    if feature_names is not None:
                        if "gene" in feat.qualifiers and feat.qualifiers["gene"][0] in feature_names:
                            fname = feat.qualifiers["gene"][0]
                        elif "gene_name" in feat.qualifiers and feat.qualifiers["gene_name"][0] in feature_names:
                            fname = feat.qualifiers["gene_name"][0]
                        elif "locus_tag" in feat.qualifiers and feat.qualifiers["locus_tag"][0] in feature_names:
                            fname = feat.qualifiers["locus_tag"][0]
                        else:
                            fname = None
                    else:
                        if "gene" in feat.qualifiers:
                            fname = feat.qualifiers["gene"][0]
                        elif "gene_name" in feat.qualifiers:
                            fname = feat.qualifiers["gene_name"][0]
                        else:
                            fname = feat.qualifiers["locus_tag"][0]
                    if feat.type == "source":
                        fname = "nuc"

                    if fname:
                        features[fname] = feat

            if feature_names is not None:
                for fe in feature_names:
                    if fe not in features:
                        print("Couldn't find gene {} in GFF or GenBank file".format(fe))

    else:
        from Bio import SeqIO
        for feat in SeqIO.read(reference, 'genbank').features:
            if feat.type=='CDS':
                if "locus_tag" in feat.qualifiers:
                    fname = feat.qualifiers["locus_tag"][0]
                    if feature_names is None or fname in feature_names:
                        features[fname] = feat
                elif "gene" in feat.qualifiers:
                    fname = feat.qualifiers["gene"][0]
                    if feature_names is None or fname in feature_names:
                        features[fname] = feat
            elif feat.type=='source': #read 'nuc' as well for annotations - need start/end of whole!
                features['nuc'] = feat

    return features

def read_config(fname):
    if not (fname and os.path.isfile(fname)):
        print("ERROR: config file %s not found."%fname)
        return defaultdict(dict)

    try:
        with open(fname, 'rb') as ifile:
            config = json.load(ifile)
    except json.decoder.JSONDecodeError as err:
        print("FATAL ERROR:")
        print("\tCouldn't parse the JSON file {}".format(fname))
        print("\tError message: '{}'".format(err.msg))
        print("\tLine number: '{}'".format(err.lineno))
        print("\tColumn number: '{}'".format(err.colno))
        print("\tYou must correct this file in order to proceed.")
        sys.exit(2)

    return config

def read_lat_longs(overrides=None, use_defaults=True):
    coordinates = {}
    # TODO: make parsing of tsv files more robust while allow for whitespace delimiting for backwards compatibility
    def add_line_to_coordinates(line):
        if line.startswith('#') or line.strip() == "":
            return
        fields = line.strip().split() if not '\t' in line else line.strip().split('\t')
        if len(fields) == 4:
            geo_field, loc = fields[0].lower(), fields[1].lower()
            lat, long = float(fields[2]), float(fields[3])
            coordinates[(geo_field, loc)] = {
                "latitude": lat,
                "longitude": long
            }
        else:
            print("WARNING: geo-coordinate file contains invalid line. Please make sure not to mix tabs and spaces as delimiters (use only tabs):",line)
    if use_defaults:
        with as_file("lat_longs.tsv") as file:
            with open(file, encoding="utf-8") as defaults:
                for line in defaults:
                    add_line_to_coordinates(line)
    if overrides:
        if os.path.isfile(overrides):
            with open(overrides, encoding='utf-8') as ifile:
                for line in ifile:
                    add_line_to_coordinates(line)
        else:
            print("WARNING: input lat/long file %s not found." % overrides)
    return coordinates

def read_colors(overrides=None, use_defaults=True):
    return ColorParser(mapping_filename=overrides, use_defaults=use_defaults).mapping

def first_line(text):
    """
    Returns the first line of the given text, ignoring leading and trailing
    whitespace.
    """
    return text.strip().splitlines()[0]


def available_cpu_cores(fallback: int = 1) -> int:
    """
    Returns the number (an int) of CPU cores available to this **process**, if
    determinable, otherwise the number of CPU cores available to the
    **computer**, if determinable, otherwise the *fallback* number (which
    defaults to 1).
    """
    try:
        # Note that this is the correct function to use, not os.cpu_count(), as
        # described in the latter's documentation.
        #
        # The reason, which the documentation does not detail, is that
        # processes may be pinned or restricted to certain CPUs by setting
        # their "affinity".  This is not typical except in high-performance
        # computing environments, but if it is done, then a computer with say
        # 24 total cores may only allow our process to use 12.  If we tried to
        # naively use all 24, we'd end up with two threads across the 12 cores.
        # This would degrade performance rather than improve it!
        assert hasattr(os, "sched_getaffinity")
        return len(os.sched_getaffinity(0))
    except:
        # cpu_count() returns None if the value is indeterminable.
        return os.cpu_count() or fallback


def nthreads_value(value):
    """
    Argument value validation and casting function for --nthreads.
    """

    if value.lower() == 'auto':
        return available_cpu_cores()

    try:
        return int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("'%s' is not an integer or the word 'auto'" % value) from None


def get_parent_name_by_child_name_for_tree(tree):
    '''
    Return dictionary mapping child node names to parent node names
    '''
    parents = {}
    for clade in tree.find_clades(order='level'):
        for child in clade:
            parents[child.name] = clade.name
    return parents


def annotate_parents_for_tree(tree):
    """Annotate each node in the given tree with its parent.

    Examples
    --------
    >>> import io
    >>> tree = Bio.Phylo.read(io.StringIO("(A, (B, C))"), "newick")
    >>> not any([hasattr(node, "parent") for node in tree.find_clades()])
    True
    >>> tree = annotate_parents_for_tree(tree)
    >>> tree.root.parent is None
    True
    >>> all([hasattr(node, "parent") for node in tree.find_clades()])
    True
    """
    tree.root.parent = None
    for node in tree.find_clades(order="level"):
        for child in node.clades:
            child.parent = node

    # Return the tree.
    return tree


def json_to_tree(json_dict, root=True, parent_cumulative_branch_length=None):
    """Returns a Bio.Phylo tree corresponding to the given JSON dictionary exported
    by `tree_to_json`.

    Assigns links back to parent nodes for the root of the tree.

    Examples
    --------

    Test opening a JSON from augur export v1.

    >>> import json
    >>> json_fh = open("tests/data/json_tree_to_nexus/flu_h3n2_ha_3y_tree.json", "r")
    >>> json_dict = json.load(json_fh)
    >>> tree = json_to_tree(json_dict)
    >>> tree.name
    'NODE_0002020'
    >>> len(tree.clades)
    2
    >>> tree.clades[0].name
    'NODE_0001489'
    >>> hasattr(tree, "attr")
    True
    >>> "dTiter" in tree.attr
    True
    >>> tree.clades[0].parent.name
    'NODE_0002020'
    >>> tree.clades[0].branch_length > 0
    True

    Test opening a JSON from augur export v2.

    >>> json_fh = open("tests/data/zika.json", "r")
    >>> json_dict = json.load(json_fh)
    >>> tree = json_to_tree(json_dict)
    >>> hasattr(tree, "name")
    True
    >>> len(tree.clades) > 0
    True
    >>> tree.clades[0].branch_length > 0
    True

    Branch lengths should be the length of the branch to each node and not the
    length from the root. The cumulative branch length from the root gets its
    own attribute.

    >>> tip = [tip for tip in tree.find_clades(terminal=True) if tip.name == "USA/2016/FLWB042"][0]
    >>> round(tip.cumulative_branch_length, 6)
    0.004747
    >>> round(tip.branch_length, 6)
    0.000186

    """
    # Check for v2 JSON which has combined metadata and tree data.
    if root and "meta" in json_dict and "tree" in json_dict:
        json_dict = json_dict["tree"]

    node = Bio.Phylo.Newick.Clade()

    # v1 and v2 JSONs use different keys for strain names.
    if "name" in json_dict:
        node.name = json_dict["name"]
    else:
        node.name = json_dict["strain"]

    # Assign all non-children attributes.
    for attr, value in json_dict.items():
        if attr != "children":
            setattr(node, attr, value)

    # Only v1 JSONs support a single `attr` attribute.
    if hasattr(node, "attr"):
        node.numdate = node.attr.get("num_date")
        node.cumulative_branch_length = node.attr.get("div")

        if "translations" in node.attr:
            node.translations = node.attr["translations"]
    elif hasattr(node, "node_attrs"):
        node.cumulative_branch_length = node.node_attrs.get("div")

    node.branch_length = 0.0
    if parent_cumulative_branch_length is not None and hasattr(node, "cumulative_branch_length"):
        node.branch_length = node.cumulative_branch_length - parent_cumulative_branch_length

    if "children" in json_dict:
        # Recursively add children to the current node.
        node.clades = [
            json_to_tree(
                child,
                root=False,
                parent_cumulative_branch_length=node.cumulative_branch_length
            )
            for child in json_dict["children"]
        ]

    if root:
        node = annotate_parents_for_tree(node)

    return node

def get_augur_version():
    """
    Returns a string of the current augur version.
    """
    return __version__


def read_bed_file(bed_file):
    """Read a BED file and return a list of excluded sites.

    Note: This function assumes the given file is a BED file. On parsing
    failures, it will attempt to skip the first line and retry, but no
    other error checking is attempted. Incorrectly formatted files will
    raise errors.

    Parameters
    ----------
    bed_file : str
        Path to the BED file

    Returns
    -------
    list of int:
        Sorted list of unique zero-indexed sites
    """
    mask_sites = []
    try:
        bed = pd.read_csv(bed_file, sep='\t', header=None, usecols=[1,2],
                          dtype={1:int,2:int})
    except ValueError:
        # Check if we have a header row. Otherwise, just fail.
        bed = pd.read_csv(bed_file, sep='\t', header=None, usecols=[1,2],
                          dtype={1:int,2:int}, skiprows=1)
        print("Skipped row 1 of %s, assuming it is a header." % bed_file)
    for _, row in bed.iterrows():
        mask_sites.extend(range(row[1], row[2]))
    return sorted(set(mask_sites))

def read_mask_file(mask_file):
    """Read a masking file and return a list of excluded sites.

    Masking files have a single masking site per line, either alone
    or as the second column of a tab-separated file. These sites
    are assumed to be one-indexed, NOT zero-indexed. Incorrectly
    formatted lines will be skipped.

    Parameters
    ----------
    mask_file : str
        Path to the masking file

    Returns
    -------
    list of int:
        Sorted list of unique zero-indexed sites
    """
    mask_sites = []
    with open(mask_file, encoding='utf-8') as mf:
        for idx, line in enumerate(l.strip() for l in mf.readlines()):
            if "\t" in line:
                line = line.split("\t")[1]
            try:
                mask_sites.append(int(line) - 1)
            except ValueError as err:
                print("Could not read line %s of %s: '%s' - %s" %
                      (idx, mask_file, line, err), file=sys.stderr)
                raise
    return sorted(set(mask_sites))

def load_mask_sites(mask_file):
    """Load masking sites from either a BED file or a masking file.

    Parameters
    ----------
    mask_file: str
        Path to the BED or masking file

    Returns
    -------
    list of int
        Sorted list of unique zero-indexed sites
    """
    if mask_file.lower().endswith(".bed"):
        mask_sites = read_bed_file(mask_file)
    else:
        mask_sites = read_mask_file(mask_file)
    print("%d masking sites read from %s" % (len(mask_sites), mask_file))
    return mask_sites

VALID_NUCLEOTIDES = { # http://reverse-complement.com/ambiguity.html
    "A", "G", "C", "T", "U", "N", "R", "Y", "S", "W", "K", "M", "B", "V", "D", "H", "-",
    "a", "g", "c", "t", "u", "n", "r", "y", "s", "w", "k", "m", "b", "v", "d", "h", "-"
}


def read_strains(*files, comment_char="#"):
    """Reads strain names from one or more plain text files and returns the
    set of distinct strains.

    Strain names can be commented with full-line or inline comments. For
    example, the following is a valid strain names file::

        # this is a comment at the top of the file
        strain1  # exclude strain1 because it isn't sequenced properly
        strain2
          # this is an empty line that will be ignored.

    Parameters
    ----------
    files : iterable of str
        one or more names of text files with one strain name per line

    Returns
    -------
    set :
        strain names from the given input files

    """
    strains = set()
    for input_file in files:
        with open_file(input_file, 'r') as ifile:
            for line in ifile:
                # Allow comments anywhere in a given line.
                strain_name = line.split(comment_char)[0].strip()
                if len(strain_name) > 0:
                    strains.add(strain_name)

    return strains
