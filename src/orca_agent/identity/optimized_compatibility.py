"""Conservative P5 connectivity/stereo compatibility, not scientific assessment."""

import math

from orca_agent.application.p5_errors import GeometryBindingMismatch

from .geometry import parse_xyz_bytes


def validate_optimized_identity(value, previous):
    """Fail closed on changed/ambiguous connections or explicitly specified stereo.

    Covalent-radius screening is a compatibility guard, not bond-order inference.
    Borderline distances are rejected rather than silently inheriting identity.
    """
    try:
        from rdkit import Chem

        symbols, coordinates = parse_xyz_bytes(value)
        expected = Chem.AddHs(Chem.MolFromSmiles(previous.canonical_isomeric_smiles))
        if symbols != tuple(a.GetSymbol() for a in expected.GetAtoms()):
            raise ValueError("atom ordering is incompatible with confirmed identity")
        radii = [
            Chem.GetPeriodicTable().GetRcovalent(a.GetAtomicNum()) for a in expected.GetAtoms()
        ]
        for i in range(len(symbols)):
            for j in range(i):
                ratio = math.dist(coordinates[i], coordinates[j]) / (radii[i] + radii[j])
                bonded = expected.GetBondBetweenAtoms(i, j) is not None
                if ratio < 0.55 or (bonded and ratio > 1.25) or (not bonded and ratio < 1.35):
                    raise ValueError("changed or ambiguous confirmed connectivity")
        observed = Chem.Mol(expected)
        Chem.RemoveStereochemistry(observed)
        conformer = Chem.Conformer(len(symbols))
        conformer.Set3D(True)
        for i, point in enumerate(coordinates):
            conformer.SetAtomPosition(i, point)
        observed.AddConformer(conformer)
        Chem.AssignStereochemistryFrom3D(observed, replaceExistingTags=True)
        Chem.AssignStereochemistry(expected, cleanIt=True, force=True)
        Chem.AssignStereochemistry(observed, cleanIt=True, force=True)
        for atom in expected.GetAtoms():
            if atom.GetChiralTag() != Chem.ChiralType.CHI_UNSPECIFIED:
                other = observed.GetAtomWithIdx(atom.GetIdx())
                if (
                    not atom.HasProp("_CIPCode")
                    or not other.HasProp("_CIPCode")
                    or atom.GetProp("_CIPCode") != other.GetProp("_CIPCode")
                ):
                    raise ValueError("specified stereocenter changed or cannot be established")
        for bond in expected.GetBonds():
            if bond.GetStereo() != Chem.BondStereo.STEREONONE:
                if observed.GetBondWithIdx(bond.GetIdx()).GetStereo() != bond.GetStereo():
                    raise ValueError("specified double-bond stereo changed or is ambiguous")
    except Exception as error:
        raise GeometryBindingMismatch(
            f"optimized identity compatibility rejected: {error}"
        ) from error
