#!/bin/bash
# wdir
#Solvate
>&2 echo 'Script running:***************************** 5. Solvation step *********************************'
cd $wdir
gmx editconf -f complex.gro -o newbox.gro -c -d 1.0 -bt cubic || { >&2 echo "Failed to run command  at line ${LINENO} of ${BASH_SOURCE}" && exit 1; }
#gmx editconf -f complex.gro -o newbox.gro -bt dodecahedron -d 1.2 #Warning about bad box - wrong number of atoms (https://gromacs.org-gmx-users.maillist.sys.kth.narkive.com/q4NXMAoY/bad-box-error) Lena version
gmx solvate -cp newbox.gro -cs spc216.gro -p topol.top -o solv.gro || { >&2 echo "Failed to run command  at line ${LINENO} of ${BASH_SOURCE}" && exit 1; }

#Add ions
>&2 echo 'Script running:***************************** 6. Ions *********************************'
#use grompp to assemble *tpr file () using any *mdp file
#here one can use *mdp for energy minization (as recommended in Justin's tutorials)
gmx grompp -f ions.mdp -c solv.gro -p topol.top -o ions.tpr   || { >&2 echo "Failed to run command  at line ${LINENO} of ${BASH_SOURCE}" && exit 1; }
#-maxwarn 10
#FATAL error SDMSO type not found -> try renaming to SDmso

gmx genion -s ions.tpr -o solv_ions.gro -p topol.top -pname NA -nname CL -neutral <<< SOL  || { >&2 echo "Failed to run command  at line ${LINENO} of ${BASH_SOURCE}" && exit 1; }