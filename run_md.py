import argparse
import os
import shutil
from glob import glob
from multiprocessing import cpu_count
import math
import time
import subprocess
from rdkit import Chem


class RawTextArgumentDefaultsHelpFormatter(argparse.RawTextHelpFormatter, argparse.ArgumentDefaultsHelpFormatter):
    pass


def filepath_type(x):
    if x:
        return os.path.abspath(x)
    else:
        return x

def init_dask_client(hostfile=None):
    if hostfile is not None:
        from dask.distributed import Client

        with open(hostfile) as f:
            hosts = [line.strip() for line in f]
        dask_client = Client(hosts[0] + ':8786', connection_limit=2048)
        # dask_client = Client()   # to test dask locally
    else:
        from dask.distributed import Client
        dask_client = Client()   # to run dask on a single server (local cluster)
        # dask_client = None
    return dask_client


def make_all_itp(fileitp_list, out_file):
    atom_type_list = []
    start_column = '[ atomtypes ]\n; name    at.num    mass    charge ptype  sigma      epsilon\n'
    for f in fileitp_list:
        with open(f) as input:
            data = input.read()
        start = data.find('[ atomtypes ]')
        end = data.find('[ moleculetype ]') - 1
        atom_type_list.extend(data[start:end].split('\n')[2:])
        # start_columns = data[start:end].split('\n')[:2]
        new_data = data[:start] + data[end + 1:]
        with open(f, 'w') as itp_ouput:
            itp_ouput.write(new_data)

    atom_type_uniq = [i for i in set(atom_type_list) if i]
    with open(out_file, 'w') as ouput:
        ouput.write(start_column)
        ouput.write('\n'.join(atom_type_uniq)+'\n')


def complex_preparation(protein_gro, ligand_gro_list, out_file):
    atoms_list = []
    with open(protein_gro) as input:
        prot_data = input.readlines()
        atoms_list.extend(prot_data[2:-1])

    for f in ligand_gro_list:
        with open(f) as input:
            data = input.readlines()
        atoms_list.extend(data[2:-1])

    n_atoms = len(atoms_list)
    with open(out_file, 'w') as output:
        output.write(prot_data[0])
        output.write(f'{n_atoms}\n')
        output.write(''.join(atoms_list))
        output.write(prot_data[-1])


def get_index(index_file):
    index_list = []
    with open(index_file) as input:
        for line in input.readlines():
            if line.startswith('['):
                index_list.append(line.replace('[','').replace(']','').strip())
    return index_list


def edit_mdp(mdp_path, couple_group, mdtime):
    subprocess.run(f"sed -i 's/tc-grps..*/tc-grps                 = {couple_group} Water_and_ions; two coupling groups/' {os.path.join(mdp_path, '*.mdp')}", shell=True)
    steps = mdtime * 1000 * 1000 / 2  # picoseconds=$mdtime*1000; femtoseconds=picoseconds*1000; steps=femtoseconds/2
    subprocess.run(f"sed -i 's/nsteps..*/nsteps                  = {steps}        ;/' {os.path.join(mdp_path, 'md.mdp')}", shell=True)


def run_complex_prep(var_lig, system_ligs, protein_gro, script_path, project_dir, mdtime):
    tec_wdir = os.path.dirname(var_lig)
    system_ligs_tec = []
    # copy system_lig itp
    for sys_lig in system_ligs:
        new_sys_lig = os.path.join(tec_wdir, os.path.basename(sys_lig))
        if os.path.isfile(f'{new_sys_lig}.itp'):
            os.remove(f'{new_sys_lig}.itp')
        shutil.copy(f'{sys_lig}.itp', f'{new_sys_lig}.itp')
        system_ligs_tec.append(new_sys_lig)
        edit_topology_file(os.path.join(tec_wdir, "topol.top"), pattern="; Include forcefield parameters",
                           add=f'; Include {os.path.basename(sys_lig)} topology\n#include "{new_sys_lig}.itp"\n',
                           how='after', n=3)

    itp_lig_list = [f'{i}.itp' for i in [var_lig]+system_ligs_tec]
    # make all itp
    make_all_itp(itp_lig_list, out_file=os.path.join(tec_wdir, 'all.itp'))
    edit_topology_file(topol_file=os.path.join(tec_wdir, "topol.top"), pattern="; Include forcefield parameters",
                       add=f'; Include all topology\n#include "{os.path.join(tec_wdir, "all.itp")}"\n', how='after', n=3)
    # complex
    gro_lig_list = [f'{i}.gro' for i in [var_lig] + system_ligs]
    complex_preparation(protein_gro=protein_gro,
                        ligand_gro_list=gro_lig_list,
                        out_file=os.path.join(tec_wdir, 'complex.gro'))
    for mdp_file in glob(os.path.join(script_path, '*.mdp')):
        shutil.copy(mdp_file, tec_wdir)


    subprocess.run(f'wdir={tec_wdir} bash {os.path.join(project_dir, "solv_ions.sh")}', shell=True)
    subprocess.run(f'cd {tec_wdir}; gmx make_ndx -f solv_ions.gro <<< "q"', shell=True)

    index_list = get_index(os.path.join(tec_wdir, 'index.ndx'))
    # index_list.index('Protein')} | {index_list.index(l_name)} | {index_list.index(c_name)
    # make couple_index_group
    couple_group_reg_ind = '|'.join([str(index_list.index(i)) for i in ['Protein']+[os.path.basename(var_lig)]+[os.path.basename(j) for j in system_ligs]])
    couple_group = '_'.join([i for i in ['Protein']+[os.path.basename(var_lig)]+[os.path.basename(j) for j in system_ligs]])
    subprocess.run(f"""
       cd {tec_wdir}
       gmx make_ndx -f solv_ions.gro -o index.ndx << INPUT
       {couple_group_reg_ind}
       q
       INPUT    
       """, shell=True)

    edit_mdp(mdp_path=tec_wdir, couple_group=couple_group, mdtime=mdtime)
    print(var_lig, 'ready')



def edit_topology_file(topol_file, pattern, add, how='before', n=0):
    with open(topol_file) as input:
        data = input.read()

    if n == 0:
        data = data.replace(pattern, f'{add}\n{pattern}' if how == 'before' else f'{pattern}\n{add}')
    else:
        data = data.split('\n')
        ind = data.index(pattern)
        data.insert(ind+n, add)
        data = '\n'.join(data)

    with open(topol_file, 'w') as output:
        output.write(data)


def prep_ligand(mol, script_path, project_dir, wdir_ligand, wdir_md, addH=True, add_to_system=False):
    mol_id = mol.GetProp('_Name')

    if len(mol_id) > 3:
        print(f'Error mol_id {mol_id}. Mol_id should be less then 3. Will be used only the first 3 letters.')
        mol_id = mol_id[:3]

    if addH:
        mol = Chem.AddHs(mol, addCoords = True)

    wdir_ligand_tec = os.path.join(wdir_ligand, mol_id)

    if add_to_system:
        wdir_md_tec = wdir_md
    else:
        wdir_md_tec = os.path.join(wdir_md, 'ligands', mol_id)

    if os.path.isfile(os.path.join(wdir_md_tec, mol_id) + '.itp'):
        print(f'{mol_id}.itp file already exist. Skip mol')
        return os.path.join(wdir_md_tec, mol_id)

    os.makedirs(wdir_ligand_tec, exist_ok=True)
    os.makedirs(wdir_md_tec, exist_ok=True)

    mol_file = os.path.join(wdir_ligand_tec, f'{mol_id}.mol')
    if not add_to_system:
        shutil.copy(os.path.join(wdir_md, "topol.top"), os.path.join(wdir_md_tec, "topol.top"))
        edit_topology_file(os.path.join(wdir_md_tec, "topol.top"), pattern="; Include forcefield parameters",
                           add=f'; Include {mol_id} topology\n#include "{os.path.join(wdir_md_tec, mol_id)}.itp"\n',
                           how='after', n=3)

    Chem.MolToMolFile(mol, mol_file)

    subprocess.run(f'script_path={script_path} lfile={mol_file} input_dirname={wdir_ligand_tec} name={mol_id} bash {os.path.join(project_dir, "lig_prep.sh")}', shell=True)

    edit_topology_file(os.path.join(wdir_md_tec, "topol.top"), pattern='; Compound        #mols',
                       add=f'{mol_id}             1', how='after', n=2)

    shutil.copy(os.path.join(wdir_ligand_tec, f'{mol_id}.itp'), os.path.join(wdir_md_tec, f'{mol_id}.itp'))
    shutil.copy(os.path.join(wdir_ligand_tec, f'{mol_id}.gro'), os.path.join(wdir_md_tec, f'{mol_id}.gro'))
    shutil.copy(os.path.join(wdir_ligand_tec, f'posre_{mol_id}.itp'), os.path.join(wdir_md_tec, f'posre_{mol_id}.itp'))

    edit_topology_file(os.path.join(wdir_md_tec, "topol.top"), pattern="; Include topology for ions",
                       add=f'; {mol_id} position restraints\n#ifdef POSRES_{mol_id}\n#include "{os.path.join(wdir_md_tec, f"posre_{mol_id}.itp")}"\n#endif\n')

    return os.path.join(wdir_md_tec, mol_id)


def supply_mols(fname):
    if fname.endswith('.sdf'):
        for n, mol in enumerate(Chem.SDMolSupplier(fname, removeHs=False)):
            if mol:
                if not mol.HasProp('_Name'):
                    mol.SetProp('_Name', f'ID{n}')
                yield mol
    if fname.endswith('.mol'):
        mol = Chem.MolFromMolFile(fname, removeHs=False)
        if mol:
            if not mol.HasProp('_Name'):
                mol.SetProp('_Name', f'ID{n}')
            yield mol


def calc_dask(func, main_arg, dask_client, dask_report_fname=None, ncpu=1, **kwargs):
    Chem.SetDefaultPickleProperties(Chem.PropertyPickleOptions.AllProps)
    if dask_client is not None:
        from dask.distributed import as_completed, performance_report
        # https://stackoverflow.com/a/12168252/895544 - optional context manager
        from contextlib import contextmanager
        none_context = contextmanager(lambda: iter([None]))()
        with (performance_report(filename=dask_report_fname) if dask_report_fname is not None else none_context):
            nworkers = len(dask_client.scheduler_info()['workers'])
            futures = []
            for i, mol in enumerate(main_arg, 1):
                futures.append(dask_client.submit(func, mol, **kwargs))
                if i == nworkers * 2:  # you may submit more tasks then workers (this is generally not necessary if you do not use priority for individual tasks)
                    break
            seq = as_completed(futures, with_results=True)
            for i, (future, results) in enumerate(seq, 1):
                yield results
                del future
                try:
                    mol = next(main_arg)
                    new_future = dask_client.submit(func, mol, **kwargs)
                    seq.add(new_future)
                except StopIteration:
                    continue


def main(protein, lfile=None, mdtime=1, system_lfile=None, wdir=None, md_param=None,
         gromacs_version="GROMACS/2021.4-foss-2020b-PLUMED-2.7.3", hostfile=None, ncpu=1):
    if wdir is None:
        wdir = os.getcwd()

    subprocess.run(f'module load {gromacs_version}', shell=True)
    project_dir = os.path.dirname(os.path.abspath(__file__))
    script_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'scripts')

    # create dirs
    wdir_protein = os.path.join(wdir, 'md_preparation', 'protein')
    wdir_ligand = os.path.join(wdir, 'md_preparation', 'var_lig')
    wdir_cofactor = os.path.join(wdir, 'md_preparation', 'system_lig')

    wdir_md = os.path.join(wdir, 'md_preparation', 'md_files')
    # wdir_md_system = os.path.join(wdir_md, 'system')

    os.makedirs(wdir_md, exist_ok=True)
    os.makedirs(wdir_protein, exist_ok=True)
    os.makedirs(wdir_ligand, exist_ok=True)
    os.makedirs(wdir_cofactor, exist_ok=True)

    # init dask
    # determine number of servers. it is assumed that ncpu is identical on all servers
    if hostfile:
        with open(hostfile) as f:
            n_servers = sum(1 if line.strip() else 0 for line in f)
    else:
        n_servers = 1

    # PART 1
    # setup calculations with more workers than servers and adjust the number of threads accordingly
    multiplicator = ncpu//2
    n_workers = n_servers * multiplicator
    n_threads = math.ceil(ncpu / multiplicator)

    # start dask cluster if hostfile was supplied
    if hostfile:
        cmd = f'dask ssh --hostfile {hostfile} --nworkers {n_workers} --nthreads {n_threads} &'
        subprocess.run(cmd, shell=True)
        time.sleep(10)

    dask_client = init_dask_client(hostfile)

    if protein is not None:
        if not os.path.isfile(protein):
            raise FileExistsError(f'{protein} does not exist')

        pname, p_ext = os.path.splitext(os.path.basename(protein))
        print(pname, p_ext)
        # check if exists
        if p_ext != '.gro':
            subprocess.run(f'gmx pdb2gmx -f {protein} -o {os.path.join(wdir_protein, pname)}.gro -water tip3p -ignh '
                      f'-i {os.path.join(wdir_md, "posre.itp")} '
                      f'-p {os.path.join(wdir_md, "topol.top")}'
                      f'<<< 6', shell=True)


    system_ligs = []
    if system_lfile is not None:
        if not os.path.isfile(system_lfile):
            raise FileExistsError(f'{system_lfile} does not exist')

        mols = supply_mols(system_lfile)

        for res1 in calc_dask(prep_ligand, mols, dask_client, ncpu=args.ncpu,
                                          script_path=script_path, project_dir=project_dir,
                                          wdir_ligand=wdir_cofactor, wdir_md=wdir_md,
                                          addH=True, add_to_system=True):
            system_ligs.append(res1)

    var_ligs = []
    if lfile is not None:
        mols = supply_mols(lfile)

        for res1 in calc_dask(prep_ligand, mols, dask_client, ncpu=args.ncpu,
                              script_path=script_path, project_dir=project_dir,
                              wdir_ligand=wdir_ligand, wdir_md=wdir_md, addH=True,
                              add_to_system=False):
            var_ligs.append(res1)

    # make all itp and create complex
    for res1 in calc_dask(run_complex_prep, iter(var_ligs), dask_client, system_ligs=system_ligs,
                          protein_gro=os.path.join(wdir_protein, f'{pname}.gro'),
                          script_path=script_path, project_dir=project_dir, mdtime=mdtime):
        pass

    if dask_client:
        dask_client.shutdown()

    # run on all cpus

    multiplicator = 1
    n_workers = n_servers * multiplicator
    n_threads = math.ceil(ncpu / multiplicator)

    # start dask cluster if hostfile was supplied
    if hostfile:
        cmd = f'dask ssh --hostfile {hostfile} --nworkers {n_workers} --nthreads {n_threads} &'
        subprocess.run(cmd, shell=True)
        time.sleep(10)

    dask_client = init_dask_client(hostfile)

    for res1 in calc_dask(lambda x: subprocess.run(f'wdir={x} bash {os.path.join(project_dir, "equlibration.sh")}', shell=True), iter([os.path.dirname(i) for i in var_ligs]),
                          dask_client):
        pass


    # run

    # for var_lig in var_ligs:
    # prep_complex(var_lig, system_ligs, protein_gro, script_path)

    if dask_client:
        dask_client.shutdown()



if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=''' ''')
    parser.add_argument('-p', '--protein', metavar='FILENAME', required=True, type=filepath_type,
                        help='input file with compound. Supported formats: *.pdb')
    parser.add_argument('-l', '--ligand', metavar='FILENAME', required=True, type=filepath_type,
                        help='input file with compound. Supported formats: *.mol')
    parser.add_argument('--cofactor', metavar='FILENAME', required=True, type=filepath_type,
                        help='input file with compound. Supported formats: *.mol')
    parser.add_argument('--hostfile', metavar='FILENAME', required=False, type=str, default=None,
                        help='text file with addresses of nodes of dask SSH cluster. The most typical, it can be '
                             'passed as $PBS_NODEFILE variable from inside a PBS script. The first line in this file '
                             'will be the address of the scheduler running on the standard port 8786. If omitted, '
                             'calculations will run on a single machine as usual.')
    parser.add_argument('-c', '--ncpu', metavar='INTEGER', required=False, default=cpu_count(), type=int,
                        help='number of CPU per server. Use all cpus by default.')

    args = parser.parse_args()

    main(protein=args.protein, lfile=args.ligand, system_lfile=args.cofactor, hostfile=args.hostfile, ncpu=args.ncpu)